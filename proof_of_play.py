#!/usr/bin/env python3
"""
rustchain-arcade: Proof of Play Session Tracking Daemon

Monitors RetroArch process activity and generates signed session heartbeats
that bridge gaming activity to RustChain mining attestation.

Features:
  - Detects running RetroArch process and identifies active ROM/core
  - Tracks session duration, achievements earned, games played
  - Generates heartbeat every 60 seconds with hardware telemetry
  - Calculates session boost multiplier (15min=1.5x, 30min+ach=2x, 60min=3x, mastery=5x)
  - Submits proof_of_play to RustChain node as attestation supplement
  - Stores session history in ~/.rustchain-arcade/sessions/
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sophia-proof-of-play")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
SESSIONS_DIR = STATE_DIR / "sessions"
CURRENT_SESSION_PATH = SESSIONS_DIR / "current_session.json"
SESSION_HISTORY_PATH = SESSIONS_DIR / "history.jsonl"

# ---------------------------------------------------------------------------
# RetroArch process detection
# ---------------------------------------------------------------------------

def find_retroarch_process(process_names: List[str]) -> Optional[Dict]:
    """Detect if RetroArch (or variant) is running.

    Returns dict with pid, cmdline, process_name or None if not found.
    Scans /proc on Linux for matching process names.
    """
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return None

    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline_path = entry / "cmdline"
            cmdline_raw = cmdline_path.read_bytes()
            if not cmdline_raw:
                continue
            # cmdline is null-separated
            cmdline_parts = cmdline_raw.decode("utf-8", errors="replace").split("\x00")
            cmdline = " ".join(p for p in cmdline_parts if p)
            exe_name = os.path.basename(cmdline_parts[0]) if cmdline_parts[0] else ""

            for pname in process_names:
                if pname.lower() in exe_name.lower() or pname.lower() in cmdline.lower():
                    return {
                        "pid": int(entry.name),
                        "cmdline": cmdline,
                        "process_name": exe_name,
                    }
        except (OSError, PermissionError, UnicodeDecodeError):
            continue

    return None


def extract_rom_info(cmdline: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract ROM path and core name from RetroArch command line.

    RetroArch typically runs as:
        retroarch -L /path/to/core.so /path/to/rom.ext
    or with --core and --rom flags.

    Returns (rom_hash, core_id) or (None, None) if not parseable.
    """
    rom_path = None
    core_id = None

    parts = cmdline.split()

    # Look for -L flag (core library)
    for i, part in enumerate(parts):
        if part == "-L" and i + 1 < len(parts):
            core_path = parts[i + 1]
            core_id = os.path.basename(core_path)
            if core_id.endswith(".so"):
                core_id = core_id[:-3]
            elif core_id.endswith(".dll"):
                core_id = core_id[:-4]
            elif core_id.endswith(".dylib"):
                core_id = core_id[:-6]
            break

    # The ROM is typically the last argument that looks like a file path
    rom_extensions = {
        ".nes", ".sfc", ".smc", ".gba", ".gbc", ".gb", ".gen", ".md", ".bin",
        ".z64", ".n64", ".v64", ".nds", ".iso", ".cue", ".chd", ".psx",
        ".a26", ".a78", ".lnx", ".pce", ".ngp", ".ngc", ".ws", ".wsc",
        ".32x", ".gg", ".sms", ".sg", ".col", ".int", ".vec",
    }
    for part in reversed(parts):
        # Strip quotes
        clean = part.strip("'\"")
        ext = os.path.splitext(clean)[1].lower()
        if ext in rom_extensions or os.path.isfile(clean):
            rom_path = clean
            break

    # Hash the ROM file for identity (first 64KB for speed)
    rom_hash = None
    if rom_path and os.path.isfile(rom_path):
        try:
            h = hashlib.sha256()
            with open(rom_path, "rb") as f:
                chunk = f.read(65536)  # First 64KB
                h.update(chunk)
            rom_hash = h.hexdigest()[:32]
        except OSError:
            rom_hash = hashlib.sha256(rom_path.encode()).hexdigest()[:32]
    elif rom_path:
        # Can't read file, hash the path as fallback
        rom_hash = hashlib.sha256(rom_path.encode()).hexdigest()[:32]

    return rom_hash, core_id


def detect_controller() -> Dict:
    """Detect connected game controllers via /dev/input and lsusb.

    Returns dict with controller info.
    """
    controllers = []

    # Check /dev/input for joystick devices
    input_dir = Path("/dev/input")
    if input_dir.exists():
        for entry in sorted(input_dir.iterdir()):
            if entry.name.startswith("js"):
                js_name = "Unknown Controller"
                js_num = entry.name[2:]
                # Try to read name from sysfs
                name_path = Path(f"/sys/class/input/js{js_num}/device/name")
                if name_path.exists():
                    try:
                        js_name = name_path.read_text().strip()
                    except OSError:
                        pass
                controllers.append({"device": entry.name, "name": js_name})

    # Also check for evdev game devices
    by_id_dir = Path("/dev/input/by-id")
    if by_id_dir.exists():
        for entry in sorted(by_id_dir.iterdir()):
            name_lower = entry.name.lower()
            if any(kw in name_lower for kw in ["joystick", "gamepad", "controller"]):
                if entry.name not in [c.get("by_id") for c in controllers]:
                    controllers.append({"by_id": entry.name})

    return {
        "connected": len(controllers) > 0,
        "count": len(controllers),
        "devices": controllers[:4],  # Cap at 4 to keep payload small
    }


def read_cpu_temp() -> Optional[float]:
    """Read CPU temperature from thermal zone (Raspberry Pi)."""
    thermal_path = Path("/sys/class/thermal/thermal_zone0/temp")
    if thermal_path.exists():
        try:
            raw = thermal_path.read_text().strip()
            return round(int(raw) / 1000.0, 1)
        except (OSError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def load_current_session() -> Optional[Dict]:
    """Load current active session state."""
    if CURRENT_SESSION_PATH.exists():
        try:
            data = json.loads(CURRENT_SESSION_PATH.read_text())
            if data.get("active"):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_current_session(session: Dict) -> None:
    """Save current session state."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_SESSION_PATH.write_text(json.dumps(session, indent=2))


def archive_session(session: Dict) -> None:
    """Archive a completed session to history."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    session["ended_at"] = datetime.now(timezone.utc).isoformat()
    session["active"] = False
    with open(SESSION_HISTORY_PATH, "a") as f:
        f.write(json.dumps(session) + "\n")
    log.info("Session archived: %s (%d heartbeats, %.1f min)",
             session.get("game_id", "unknown"),
             session.get("heartbeat_count", 0),
             session.get("duration_minutes", 0))


def calculate_boost_multiplier(
    duration_seconds: float,
    achievements_earned: int,
    has_mastery: bool,
    boost_config: Dict,
) -> float:
    """Calculate session boost multiplier based on play activity.

    Tiers:
      - 15min played: 1.5x
      - 30min + at least 1 achievement: 2.0x
      - 60min played: 3.0x
      - Mastery unlocked (victory lap): 5.0x
    """
    if has_mastery:
        return boost_config.get("mastery_victory_lap", 5.0)

    duration_min = duration_seconds / 60.0

    if duration_min >= 60.0:
        return boost_config.get("60min", 3.0)

    if duration_min >= 30.0 and achievements_earned >= 1:
        return boost_config.get("30min_with_achievement", 2.0)

    if duration_min >= 15.0:
        return boost_config.get("15min", 1.5)

    return 1.0


def create_heartbeat(session: Dict, config: Dict) -> Dict:
    """Generate a signed session heartbeat.

    Contains: timestamp, ROM hash, game_id, controller state, cpu_temp, uptime.
    """
    now = time.time()
    uptime_seconds = now - session.get("started_at_ts", now)

    controller = detect_controller()
    cpu_temp = read_cpu_temp()

    heartbeat = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ts": int(now),
        "session_id": session.get("session_id", ""),
        "rom_hash": session.get("rom_hash"),
        "game_id": session.get("game_id"),
        "core_id": session.get("core_id"),
        "controller_connected": controller["connected"],
        "controller_count": controller["count"],
        "cpu_temp": cpu_temp,
        "uptime_seconds": round(uptime_seconds, 1),
        "heartbeat_seq": session.get("heartbeat_count", 0) + 1,
        "achievements_this_session": session.get("achievements_earned", 0),
        "boost_multiplier": session.get("boost_multiplier", 1.0),
    }

    # Sign the heartbeat with a simple HMAC-like hash
    wallet_id = os.environ.get("SOPHIA_WALLET", config.get("node_id", "rustchain-arcade-rpi"))
    sign_data = f"{heartbeat['ts']}:{heartbeat['session_id']}:{heartbeat['heartbeat_seq']}:{wallet_id}"
    heartbeat["signature"] = hashlib.sha256(sign_data.encode()).hexdigest()[:32]

    return heartbeat


def submit_heartbeat(heartbeat: Dict, config: Dict) -> bool:
    """Submit heartbeat to RustChain node."""
    node_url = config["rustchain"]["node_url"].rstrip("/")
    verify_ssl = config["rustchain"].get("verify_ssl", False)
    wallet_id = os.environ.get("SOPHIA_WALLET", config.get("node_id", "rustchain-arcade-rpi"))

    payload = {
        "miner": wallet_id,
        "type": "proof_of_play_heartbeat",
        "heartbeat": heartbeat,
    }

    url = f"{node_url}/api/gaming/heartbeat"
    try:
        resp = requests.post(url, json=payload, verify=verify_ssl, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            log.debug("Heartbeat submit HTTP %d: %s", resp.status_code, resp.text[:100])
    except requests.RequestException as e:
        log.debug("Heartbeat submit failed: %s", e)

    return False


def submit_proof_of_play(session: Dict, config: Dict) -> bool:
    """Submit full proof_of_play attestation to the RustChain node.

    Called when session ends or on attestation cycle.
    """
    node_url = config["rustchain"]["node_url"].rstrip("/")
    verify_ssl = config["rustchain"].get("verify_ssl", False)
    wallet_id = os.environ.get("SOPHIA_WALLET", config.get("node_id", "rustchain-arcade-rpi"))

    payload = {
        "miner": wallet_id,
        "type": "proof_of_play",
        "session": {
            "session_id": session.get("session_id", ""),
            "rom_hash": session.get("rom_hash"),
            "game_id": session.get("game_id"),
            "core_id": session.get("core_id"),
            "started_at": session.get("started_at"),
            "duration_minutes": session.get("duration_minutes", 0),
            "heartbeat_count": session.get("heartbeat_count", 0),
            "achievements_earned": session.get("achievements_earned", 0),
            "boost_multiplier": session.get("boost_multiplier", 1.0),
            "controller_connected": session.get("controller_connected", False),
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    url = f"{node_url}/api/gaming/proof_of_play"
    try:
        resp = requests.post(url, json=payload, verify=verify_ssl, timeout=15)
        if resp.status_code == 200:
            log.info("Proof of Play submitted: %.1f min, %d heartbeats, boost=%.1fx",
                     session.get("duration_minutes", 0),
                     session.get("heartbeat_count", 0),
                     session.get("boost_multiplier", 1.0))
            return True
        else:
            log.debug("Proof of Play submit HTTP %d", resp.status_code)
    except requests.RequestException as e:
        log.debug("Proof of Play submit failed: %s", e)

    return False


def count_recent_achievements() -> int:
    """Count achievements earned in the current session window.

    Reads from the achievement bridge's velocity tracker.
    """
    velocity_path = STATE_DIR / "velocity_tracker.json"
    if not velocity_path.exists():
        return 0
    try:
        data = json.loads(velocity_path.read_text())
        # Count timestamps from last session start
        session = load_current_session()
        if session and session.get("started_at_ts"):
            session_start = session["started_at_ts"]
            recent = [ts for ts in data.get("timestamps", []) if ts >= session_start]
            return len(recent)
        return len(data.get("timestamps", []))
    except (json.JSONDecodeError, OSError):
        return 0


def check_mastery_this_session() -> bool:
    """Check if a mastery/victory lap was activated during this session."""
    victory_path = STATE_DIR / "victory_lap.json"
    if not victory_path.exists():
        return False
    try:
        data = json.loads(victory_path.read_text())
        return data.get("active", False) and not data.get("epoch_used", False)
    except (json.JSONDecodeError, OSError):
        return False


# ---------------------------------------------------------------------------
# Main session tracking loop
# ---------------------------------------------------------------------------

def session_loop(config: Dict) -> None:
    """Main loop: detect RetroArch, track sessions, generate heartbeats."""
    pop_cfg = config.get("proof_of_play", {})
    heartbeat_interval = pop_cfg.get("heartbeat_interval_seconds", 60)
    process_names = pop_cfg.get("retroarch_process_names", ["retroarch", "retroarch-core"])
    boost_config = pop_cfg.get("session_boost_multipliers", {})

    log.info("=== Sophia Proof of Play Daemon ===")
    log.info("Heartbeat interval: %ds", heartbeat_interval)
    log.info("Watching for: %s", ", ".join(process_names))

    # Check for leftover active session from crash/restart
    leftover = load_current_session()
    if leftover and leftover.get("active"):
        log.info("Found leftover session from previous run, archiving...")
        archive_session(leftover)
        CURRENT_SESSION_PATH.unlink(missing_ok=True)

    while True:
        try:
            ra_proc = find_retroarch_process(process_names)

            if ra_proc is None:
                # No RetroArch running -- clear session if one was active
                current = load_current_session()
                if current and current.get("active"):
                    now = time.time()
                    current["duration_minutes"] = round(
                        (now - current.get("started_at_ts", now)) / 60.0, 1
                    )
                    submit_proof_of_play(current, config)
                    archive_session(current)
                    # Clear current session file
                    save_current_session({
                        "active": False,
                        "boost_multiplier": 1.0,
                    })
                    log.info("RetroArch stopped. Session ended.")

                time.sleep(heartbeat_interval)
                continue

            # RetroArch is running
            current = load_current_session()

            if current is None or not current.get("active"):
                # Start new session
                rom_hash, core_id = extract_rom_info(ra_proc["cmdline"])
                controller = detect_controller()
                session_id = hashlib.sha256(
                    f"{time.time()}:{ra_proc['pid']}:{rom_hash or 'unknown'}".encode()
                ).hexdigest()[:16]

                now = time.time()
                current = {
                    "active": True,
                    "session_id": session_id,
                    "pid": ra_proc["pid"],
                    "process_name": ra_proc["process_name"],
                    "rom_hash": rom_hash,
                    "game_id": rom_hash[:8] if rom_hash else None,
                    "core_id": core_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "started_at_ts": now,
                    "heartbeat_count": 0,
                    "achievements_earned": 0,
                    "controller_connected": controller["connected"],
                    "boost_multiplier": 1.0,
                    "duration_minutes": 0.0,
                }
                save_current_session(current)
                log.info("New session started: ROM=%s Core=%s Controller=%s",
                         rom_hash or "unknown", core_id or "unknown",
                         "YES" if controller["connected"] else "NO")

            # Update session state
            now = time.time()
            duration_s = now - current.get("started_at_ts", now)
            current["duration_minutes"] = round(duration_s / 60.0, 1)

            # Count achievements earned during this session
            current["achievements_earned"] = count_recent_achievements()

            # Check mastery
            has_mastery = check_mastery_this_session()

            # Calculate boost
            current["boost_multiplier"] = calculate_boost_multiplier(
                duration_s,
                current["achievements_earned"],
                has_mastery,
                boost_config,
            )

            # Update controller state
            controller = detect_controller()
            current["controller_connected"] = controller["connected"]

            # Generate and submit heartbeat
            heartbeat = create_heartbeat(current, config)
            submitted = submit_heartbeat(heartbeat, config)
            current["heartbeat_count"] = current.get("heartbeat_count", 0) + 1

            status_char = "+" if submitted else "."
            log.info("  [%s] Heartbeat #%d | %.1f min | ach:%d | boost:%.1fx | ctrl:%s",
                     status_char,
                     current["heartbeat_count"],
                     current["duration_minutes"],
                     current["achievements_earned"],
                     current["boost_multiplier"],
                     "YES" if current["controller_connected"] else "NO")

            save_current_session(current)

        except Exception:
            log.exception("Error in session tracking loop")

        time.sleep(heartbeat_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_config() -> Dict:
    """Load config from file."""
    cfg_path = Path(CONFIG_PATH)
    if cfg_path.exists():
        with open(cfg_path) as f:
            return json.load(f)
    else:
        log.error("Config not found at %s", cfg_path)
        sys.exit(1)


def main():
    config = load_config()
    if not config.get("proof_of_play", {}).get("enabled", True):
        log.info("Proof of Play is disabled in config. Exiting.")
        sys.exit(0)

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        session_loop(config)
    except KeyboardInterrupt:
        log.info("Proof of Play daemon stopped by user.")
        # Archive any active session
        current = load_current_session()
        if current and current.get("active"):
            archive_session(current)


if __name__ == "__main__":
    main()
