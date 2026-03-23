#!/usr/bin/env python3
"""
rustchain-arcade: RustChain Miner for Raspberry Pi 4/5

Detects RPi hardware (BCM2711/BCM2712), runs fingerprint checks,
and submits attestation to the RustChain network on a configurable interval.

ARM devices earn 0.0005x weight (server-enforced). This is honest reporting --
real hardware, real attestation, real (tiny) rewards.

Proof of Play integration: if a gaming session is active, includes session
data in attestation for boosted mining weight (up to 5.0x during victory lap).
"""

import asyncio
import hashlib
import json
import logging
import os
import platform
import random
import socket
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sophia-miner")

# ---------------------------------------------------------------------------
# Paths & defaults
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
MINER_STATE = STATE_DIR / "miner_state.json"

# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

def read_cpuinfo() -> str:
    """Read /proc/cpuinfo as a string."""
    try:
        with open("/proc/cpuinfo", "r") as f:
            return f.read()
    except OSError:
        return ""


def detect_rpi_model(cpuinfo: str) -> Tuple[str, str]:
    """Return (cpu_model, cpu_brand) for Raspberry Pi.

    BCM2711 = RPi 4, BCM2712 = RPi 5.
    Falls back to generic ARM detection.
    """
    cpu_model = "unknown_arm"
    cpu_brand = ""

    for line in cpuinfo.splitlines():
        lower = line.lower()
        if "hardware" in lower and ":" in lower:
            hw = line.split(":", 1)[1].strip()
            if "bcm2712" in hw.lower():
                cpu_model = "BCM2712"
                cpu_brand = "Raspberry Pi 5"
            elif "bcm2711" in hw.lower():
                cpu_model = "BCM2711"
                cpu_brand = "Raspberry Pi 4"
            elif "bcm" in hw.lower():
                cpu_model = hw
                cpu_brand = f"Raspberry Pi ({hw})"
            else:
                cpu_model = hw
                cpu_brand = hw
        if "model name" in lower and ":" in lower:
            cpu_brand = cpu_brand or line.split(":", 1)[1].strip()

    if not cpu_brand:
        cpu_brand = platform.processor() or platform.machine()

    return cpu_model, cpu_brand


def get_mac_addresses() -> List[str]:
    """Collect MAC addresses from network interfaces (exclude lo)."""
    macs = []
    net_dir = Path("/sys/class/net")
    if net_dir.exists():
        for iface in net_dir.iterdir():
            if iface.name == "lo":
                continue
            addr_file = iface / "address"
            try:
                mac = addr_file.read_text().strip()
                if mac and mac != "00:00:00:00:00:00":
                    macs.append(mac)
            except OSError:
                continue
    return sorted(set(macs))


# ---------------------------------------------------------------------------
# Fingerprint checks (ARM-appropriate subset)
# ---------------------------------------------------------------------------

def check_clock_drift(samples: int = 2000) -> Dict:
    """Measure oscillator drift via high-res timing.

    Real hardware has measurable jitter; VMs tend to be too uniform.
    """
    deltas = []
    for _ in range(samples):
        t0 = time.perf_counter_ns()
        # Tiny busy-wait to capture oscillator variance
        _ = sum(range(50))
        t1 = time.perf_counter_ns()
        deltas.append(t1 - t0)

    if not deltas:
        return {"passed": False, "data": {"reason": "no_samples"}}

    mean_d = sum(deltas) / len(deltas)
    if mean_d == 0:
        return {"passed": False, "data": {"cv": 0, "reason": "zero_mean"}}

    variance = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
    std_dev = variance ** 0.5
    cv = std_dev / mean_d

    # Real hardware typically has cv > 0.01; VMs can be < 0.001
    passed = cv > 0.001
    return {
        "passed": passed,
        "data": {
            "cv": round(cv, 6),
            "mean_ns": round(mean_d, 2),
            "std_ns": round(std_dev, 2),
            "samples": samples,
        },
    }


def check_thermal_drift() -> Dict:
    """Read RPi thermal zone and derive entropy from fluctuations.

    /sys/class/thermal/thermal_zone0/temp gives millidegrees C on RPi.
    """
    thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
    if not thermal_path.exists():
        return {"passed": True, "data": {"reason": "no_thermal_sensor", "temp_c": None}}

    readings = []
    for _ in range(20):
        try:
            raw = thermal_path.read_text().strip()
            readings.append(int(raw))
        except (OSError, ValueError):
            pass
        time.sleep(0.05)

    if len(readings) < 5:
        return {"passed": True, "data": {"reason": "insufficient_readings"}}

    mean_t = sum(readings) / len(readings)
    variance = sum((r - mean_t) ** 2 for r in readings) / len(readings)
    temp_c = round(mean_t / 1000.0, 1)

    return {
        "passed": True,
        "data": {
            "temp_c": temp_c,
            "variance": round(variance, 2),
            "readings": len(readings),
        },
    }


def check_anti_emulation() -> Dict:
    """Detect VM/emulator indicators.

    Checks DMI, cpuinfo hypervisor flag, and known VM signatures.
    """
    vm_indicators = []

    # Check /sys/class/dmi for VM vendors
    dmi_paths = {
        "/sys/class/dmi/id/sys_vendor": ["qemu", "vmware", "virtualbox", "xen", "microsoft", "parallels"],
        "/sys/class/dmi/id/product_name": ["qemu", "vmware", "virtualbox", "virtual machine"],
        "/sys/class/dmi/id/board_vendor": ["qemu", "vmware", "oracle"],
    }
    for path, keywords in dmi_paths.items():
        try:
            content = Path(path).read_text().strip().lower()
            for kw in keywords:
                if kw in content:
                    vm_indicators.append(f"{path}:{kw}")
        except OSError:
            pass

    # Check cpuinfo for hypervisor flag
    cpuinfo = read_cpuinfo().lower()
    if "hypervisor" in cpuinfo:
        vm_indicators.append("cpuinfo:hypervisor")

    # Check /proc/scsi/scsi for VM storage
    try:
        scsi = Path("/proc/scsi/scsi").read_text().lower()
        for kw in ["qemu", "vmware", "vbox", "virtual"]:
            if kw in scsi:
                vm_indicators.append(f"/proc/scsi/scsi:{kw}")
    except OSError:
        pass

    # Check for Docker/LXC/container indicators
    try:
        cgroup = Path("/proc/1/cgroup").read_text().lower()
        for kw in ["docker", "lxc", "kubepods"]:
            if kw in cgroup:
                vm_indicators.append(f"cgroup:{kw}")
    except OSError:
        pass

    passed = len(vm_indicators) == 0
    return {
        "passed": passed,
        "data": {
            "vm_indicators": vm_indicators,
            "is_real_hardware": passed,
        },
    }


def run_fingerprint_checks() -> Dict:
    """Run the ARM-appropriate fingerprint checks.

    Skips SIMD (no AltiVec on ARM) and ROM checks (not retro platform).
    Runs: clock drift, thermal drift, anti-emulation.
    """
    log.info("Running fingerprint checks...")

    clock = check_clock_drift()
    log.info("  Clock drift: %s (cv=%.6f)", "PASS" if clock["passed"] else "FAIL", clock["data"].get("cv", 0))

    thermal = check_thermal_drift()
    log.info("  Thermal drift: %s (temp=%.1f C)", "PASS" if thermal["passed"] else "FAIL", thermal["data"].get("temp_c", 0) or 0)

    anti_emu = check_anti_emulation()
    log.info("  Anti-emulation: %s", "PASS" if anti_emu["passed"] else "FAIL")

    all_passed = clock["passed"] and thermal["passed"] and anti_emu["passed"]
    log.info("  Overall: %s", "ALL PASSED" if all_passed else "FAILED")

    return {
        "all_passed": all_passed,
        "checks": {
            "clock_drift": clock,
            "thermal_drift": thermal,
            "anti_emulation": anti_emu,
        },
    }


# ---------------------------------------------------------------------------
# Miner ID / wallet helpers
# ---------------------------------------------------------------------------

def load_or_create_miner_id() -> str:
    """Load persistent miner ID from state, or create one."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if MINER_STATE.exists():
        try:
            state = json.loads(MINER_STATE.read_text())
            if "miner_id" in state:
                return state["miner_id"]
        except (json.JSONDecodeError, OSError):
            pass

    # Generate from hostname + MAC for stability across reboots
    hostname = socket.gethostname()
    macs = get_mac_addresses()
    seed = f"{hostname}:{':'.join(macs)}:{uuid.getnode()}"
    miner_id = hashlib.sha256(seed.encode()).hexdigest()[:16]

    state = {}
    if MINER_STATE.exists():
        try:
            state = json.loads(MINER_STATE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    state["miner_id"] = miner_id
    MINER_STATE.write_text(json.dumps(state, indent=2))
    return miner_id


def get_wallet_id(config: Dict) -> str:
    """Return wallet ID from env, config, or generate from node_id."""
    wallet = os.environ.get("SOPHIA_WALLET")
    if wallet:
        return wallet
    node_id = config.get("node_id", "rustchain-arcade-rpi")
    return node_id


# ---------------------------------------------------------------------------
# Proof of Play integration
# ---------------------------------------------------------------------------

def _read_proof_of_play_session() -> Optional[Dict]:
    """Read current proof_of_play session state from the PoP daemon.

    The proof_of_play.py daemon writes session state to
    ~/.rustchain-arcade/sessions/current_session.json which we read here.
    Returns session dict if active, None otherwise.
    """
    session_file = STATE_DIR / "sessions" / "current_session.json"
    if not session_file.exists():
        return None

    try:
        data = json.loads(session_file.read_text())
        if data.get("active", False):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Attestation submission
# ---------------------------------------------------------------------------

async def submit_attestation(
    session: aiohttp.ClientSession,
    config: Dict,
    fingerprint: Dict,
) -> bool:
    """Build and submit attestation payload to RustChain node."""
    cpuinfo = read_cpuinfo()
    cpu_model, cpu_brand = detect_rpi_model(cpuinfo)
    macs = get_mac_addresses()
    miner_id = load_or_create_miner_id()
    wallet_id = get_wallet_id(config)
    nonce = hashlib.sha256(
        f"{miner_id}:{time.time()}:{random.random()}".encode()
    ).hexdigest()[:32]

    # Read proof_of_play session data if available
    proof_of_play = _read_proof_of_play_session()
    session_boost = proof_of_play.get("boost_multiplier", 1.0) if proof_of_play else 1.0
    heartbeat_count = proof_of_play.get("heartbeat_count", 0) if proof_of_play else 0

    attestation = {
        "miner": wallet_id,
        "miner_id": miner_id,
        "nonce": nonce,
        "report": {
            "arch": "aarch64",
            "cpu": cpu_brand,
        },
        "device": {
            "device_model": cpu_model,
            "device_arch": "aarch64",
            "device_family": "ARM",
            "machine": platform.machine(),
        },
        "signals": {
            "macs": macs,
        },
        "fingerprint": fingerprint,
        "proof_of_play": {
            "active": proof_of_play is not None and proof_of_play.get("active", False),
            "session_boost_multiplier": session_boost,
            "heartbeat_count": heartbeat_count,
            "session_id": proof_of_play.get("session_id", "") if proof_of_play else "",
            "duration_minutes": proof_of_play.get("duration_minutes", 0) if proof_of_play else 0,
            "achievements_earned": proof_of_play.get("achievements_earned", 0) if proof_of_play else 0,
        },
    }

    node_url = config["rustchain"]["node_url"].rstrip("/")
    url = f"{node_url}/attest/submit"
    verify_ssl = config["rustchain"].get("verify_ssl", False)

    ssl_ctx = None if verify_ssl else False

    try:
        async with session.post(url, json=attestation, ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            body = await resp.text()
            if resp.status == 200:
                log.info("Attestation accepted: %s", body[:200])
                return True
            else:
                log.warning("Attestation rejected (HTTP %d): %s", resp.status, body[:300])
                return False
    except aiohttp.ClientError as e:
        log.error("Network error submitting attestation: %s", e)
        return False
    except asyncio.TimeoutError:
        log.error("Timeout submitting attestation")
        return False


# ---------------------------------------------------------------------------
# Main mining loop
# ---------------------------------------------------------------------------

async def mining_loop(config: Dict) -> None:
    """Run attestation on a fixed interval."""
    interval = config.get("mining", {}).get("interval_seconds", 600)
    node_id = config.get("node_id", "rustchain-arcade-rpi")

    cpuinfo = read_cpuinfo()
    cpu_model, cpu_brand = detect_rpi_model(cpuinfo)
    log.info("=== Sophia Edge Miner v2.0 ===")
    log.info("Node ID   : %s", node_id)
    log.info("Wallet    : %s", get_wallet_id(config))
    log.info("Miner ID  : %s", load_or_create_miner_id())
    log.info("CPU       : %s (%s)", cpu_brand, cpu_model)
    log.info("Arch      : %s", platform.machine())
    log.info("Interval  : %ds", interval)
    log.info("Node URL  : %s", config["rustchain"]["node_url"])
    log.info("Base ARM weight: 0.0005x (boosted up to 5x during active play)")

    async with aiohttp.ClientSession() as session:
        while True:
            fingerprint = run_fingerprint_checks()

            # Check for active gaming session
            pop_session = _read_proof_of_play_session()
            if pop_session and pop_session.get("active"):
                boost = pop_session.get("boost_multiplier", 1.0)
                log.info("Gaming session active: boost=%.1fx, heartbeats=%d, ach=%d",
                         boost,
                         pop_session.get("heartbeat_count", 0),
                         pop_session.get("achievements_earned", 0))

            ok = await submit_attestation(session, config, fingerprint)
            if ok:
                log.info("Next attestation in %ds", interval)
            else:
                log.warning("Will retry in %ds", interval)
            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_config() -> Dict:
    """Load config from file, with environment overrides."""
    cfg_path = Path(CONFIG_PATH)
    if cfg_path.exists():
        with open(cfg_path) as f:
            config = json.load(f)
    else:
        log.warning("Config not found at %s, using defaults", cfg_path)
        config = {
            "node_id": os.environ.get("SOPHIA_NODE_ID", "rustchain-arcade-rpi"),
            "rustchain": {
                "node_url": os.environ.get("SOPHIA_NODE_URL", "https://50.28.86.131"),
                "verify_ssl": False,
            },
            "mining": {
                "enabled": True,
                "interval_seconds": 600,
            },
        }

    # Environment overrides
    if os.environ.get("SOPHIA_NODE_URL"):
        config["rustchain"]["node_url"] = os.environ["SOPHIA_NODE_URL"]
    if os.environ.get("SOPHIA_NODE_ID"):
        config["node_id"] = os.environ["SOPHIA_NODE_ID"]

    return config


def main():
    config = load_config()
    if not config.get("mining", {}).get("enabled", True):
        log.info("Mining is disabled in config. Exiting.")
        sys.exit(0)
    try:
        asyncio.run(mining_loop(config))
    except KeyboardInterrupt:
        log.info("Miner stopped by user.")


if __name__ == "__main__":
    main()
