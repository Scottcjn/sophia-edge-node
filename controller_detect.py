#!/usr/bin/env python3
"""
rustchain-arcade: Controller Authentication and Detection

Detects connected game controllers via /dev/input/event* and
/proc/bus/input/devices, identifies known vintage controllers by USB ID,
and reports controller type in proof-of-play heartbeats.

Known vintage controller USB IDs with authenticity bonuses are tracked.

CLI usage:
    python3 controller_detect.py
    python3 controller_detect.py --json
    python3 controller_detect.py --watch
"""

import argparse
import json
import logging
import os
import re
import struct
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sophia-controller")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
CONTROLLER_STATE_PATH = STATE_DIR / "controller_state.json"

# ---------------------------------------------------------------------------
# Known controller USB IDs and metadata
# ---------------------------------------------------------------------------

# Format: (vendor_id, product_id): {name, type, era, authenticity_class}
# authenticity_class determines bonus eligibility:
#   "vintage_replica" - modern USB replica of classic controller (bonus eligible)
#   "vintage_adapter" - adapter for original hardware controllers (highest bonus)
#   "modern_retro"    - modern controller with retro styling
#   "standard"        - modern generic controller (no bonus)

KNOWN_CONTROLLERS: Dict[Tuple[int, int], Dict] = {
    # 8BitDo controllers (vintage replicas)
    (0x2dc8, 0x6001): {
        "name": "8BitDo SN30 Pro",
        "type": "snes_replica",
        "era": "SNES",
        "authenticity_class": "vintage_replica",
    },
    (0x2dc8, 0x6002): {
        "name": "8BitDo SN30 Pro+",
        "type": "snes_replica",
        "era": "SNES",
        "authenticity_class": "vintage_replica",
    },
    (0x2dc8, 0x2101): {
        "name": "8BitDo SN30",
        "type": "snes_replica",
        "era": "SNES",
        "authenticity_class": "vintage_replica",
    },
    (0x2dc8, 0x3820): {
        "name": "8BitDo Pro 2",
        "type": "modern_retro",
        "era": "Multi",
        "authenticity_class": "modern_retro",
    },

    # iBuffalo (classic SNES replica)
    (0x0583, 0x2060): {
        "name": "iBuffalo SNES Controller",
        "type": "snes_replica",
        "era": "SNES",
        "authenticity_class": "vintage_replica",
    },

    # Raphnet adapters (original hardware adapters -- highest authenticity)
    (0x289b, 0x0058): {
        "name": "Raphnet N64 Adapter",
        "type": "n64_adapter",
        "era": "N64",
        "authenticity_class": "vintage_adapter",
    },
    (0x289b, 0x0001): {
        "name": "Raphnet SNES Adapter",
        "type": "snes_adapter",
        "era": "SNES",
        "authenticity_class": "vintage_adapter",
    },
    (0x289b, 0x0005): {
        "name": "Raphnet Genesis Adapter",
        "type": "genesis_adapter",
        "era": "Genesis",
        "authenticity_class": "vintage_adapter",
    },
    (0x289b, 0x000C): {
        "name": "Raphnet GameCube Adapter",
        "type": "gamecube_adapter",
        "era": "GameCube",
        "authenticity_class": "vintage_adapter",
    },

    # RetroFlag controllers
    (0x0079, 0x0011): {
        "name": "RetroFlag SNES Controller",
        "type": "snes_replica",
        "era": "SNES",
        "authenticity_class": "vintage_replica",
    },

    # Generic SNES USB gamepads (DragonRise / cheap clones)
    (0x0079, 0x0006): {
        "name": "Generic SNES USB Gamepad",
        "type": "snes_clone",
        "era": "SNES",
        "authenticity_class": "vintage_replica",
    },
    (0x0079, 0x0011): {
        "name": "Generic USB Gamepad (SNES-style)",
        "type": "snes_clone",
        "era": "SNES",
        "authenticity_class": "vintage_replica",
    },

    # Sony controllers
    (0x054c, 0x0268): {
        "name": "PlayStation 3 DualShock 3",
        "type": "ps3",
        "era": "PS3",
        "authenticity_class": "standard",
    },
    (0x054c, 0x05c4): {
        "name": "PlayStation 4 DualShock 4",
        "type": "ps4",
        "era": "PS4",
        "authenticity_class": "standard",
    },
    (0x054c, 0x0ce6): {
        "name": "PlayStation 5 DualSense",
        "type": "ps5",
        "era": "PS5",
        "authenticity_class": "standard",
    },

    # Microsoft Xbox controllers
    (0x045e, 0x028e): {
        "name": "Xbox 360 Controller",
        "type": "xbox360",
        "era": "Xbox 360",
        "authenticity_class": "standard",
    },
    (0x045e, 0x02d1): {
        "name": "Xbox One Controller",
        "type": "xone",
        "era": "Xbox One",
        "authenticity_class": "standard",
    },
    (0x045e, 0x0b12): {
        "name": "Xbox Series X|S Controller",
        "type": "xsx",
        "era": "Xbox Series",
        "authenticity_class": "standard",
    },

    # Nintendo
    (0x057e, 0x2009): {
        "name": "Nintendo Switch Pro Controller",
        "type": "switch_pro",
        "era": "Switch",
        "authenticity_class": "standard",
    },
    (0x057e, 0x2006): {
        "name": "Nintendo Joy-Con (L)",
        "type": "joycon_l",
        "era": "Switch",
        "authenticity_class": "standard",
    },
    (0x057e, 0x2007): {
        "name": "Nintendo Joy-Con (R)",
        "type": "joycon_r",
        "era": "Switch",
        "authenticity_class": "standard",
    },

    # Valve
    (0x28de, 0x1142): {
        "name": "Steam Controller",
        "type": "steam",
        "era": "Modern",
        "authenticity_class": "standard",
    },

    # Retro adapters (Mayflash, etc.)
    (0x0e8f, 0x3013): {
        "name": "Mayflash N64 Adapter",
        "type": "n64_adapter",
        "era": "N64",
        "authenticity_class": "vintage_adapter",
    },
    (0x0810, 0x0001): {
        "name": "USB Gamepad (retro adapter)",
        "type": "generic_adapter",
        "era": "Multi",
        "authenticity_class": "vintage_adapter",
    },
}

# Authenticity class to bonus multiplier mapping
AUTHENTICITY_BONUSES = {
    "vintage_adapter": 1.08,    # Original hardware through adapter -- max bonus
    "vintage_replica": 1.05,    # Quality replica (8BitDo, iBuffalo, etc.)
    "modern_retro": 1.02,       # Modern controller with retro styling
    "standard": 1.0,            # No bonus
}


# ---------------------------------------------------------------------------
# Controller detection from /proc/bus/input/devices
# ---------------------------------------------------------------------------

def parse_input_devices() -> List[Dict]:
    """Parse /proc/bus/input/devices to find all input devices.

    Returns list of dicts with: name, vendor, product, handlers, phys.
    """
    devices_path = Path("/proc/bus/input/devices")
    if not devices_path.exists():
        return []

    try:
        content = devices_path.read_text()
    except OSError:
        return []

    devices = []
    current = {}

    for line in content.split("\n"):
        line = line.strip()
        if not line:
            if current:
                devices.append(current)
                current = {}
            continue

        if line.startswith("I:"):
            # Bus=0003 Vendor=054c Product=0268 Version=0111
            match = re.search(
                r"Vendor=([0-9a-fA-F]+)\s+Product=([0-9a-fA-F]+)", line
            )
            if match:
                current["vendor"] = int(match.group(1), 16)
                current["product"] = int(match.group(2), 16)
            bus_match = re.search(r"Bus=([0-9a-fA-F]+)", line)
            if bus_match:
                current["bus"] = int(bus_match.group(1), 16)

        elif line.startswith("N:"):
            # N: Name="Sony PLAYSTATION(R)3 Controller"
            match = re.search(r'Name="(.+)"', line)
            if match:
                current["name"] = match.group(1)

        elif line.startswith("P:"):
            # P: Phys=usb-0000:00:14.0-1/input0
            match = re.search(r"Phys=(.+)", line)
            if match:
                current["phys"] = match.group(1)

        elif line.startswith("H:"):
            # H: Handlers=event0 js0
            match = re.search(r"Handlers=(.+)", line)
            if match:
                current["handlers"] = match.group(1).split()

    # Don't forget the last device
    if current:
        devices.append(current)

    return devices


def is_gamepad_device(device: Dict) -> bool:
    """Determine if an input device is a game controller.

    Checks for js* handler or known gamepad indicators.
    """
    handlers = device.get("handlers", [])

    # Has a joystick handler
    if any(h.startswith("js") for h in handlers):
        return True

    # Check name for gamepad keywords
    name = device.get("name", "").lower()
    gamepad_keywords = [
        "gamepad", "joystick", "controller", "joycon", "dualshock",
        "dualsense", "xbox", "8bitdo", "snes", "nes", "n64", "genesis",
        "retro", "arcade", "fight", "raphnet", "ibuffalo", "mayflash",
    ]
    if any(kw in name for kw in gamepad_keywords):
        return True

    return False


def detect_event_devices() -> List[Dict]:
    """Detect game controllers from /dev/input/event* devices.

    Uses /proc/bus/input/devices for metadata, cross-references
    with /dev/input/event* existence.
    """
    all_devices = parse_input_devices()
    controllers = []

    for device in all_devices:
        if not is_gamepad_device(device):
            continue

        vendor = device.get("vendor", 0)
        product = device.get("product", 0)
        name = device.get("name", "Unknown Controller")
        handlers = device.get("handlers", [])

        # Find the event device and js device paths
        event_device = None
        js_device = None
        for h in handlers:
            if h.startswith("event"):
                event_path = Path(f"/dev/input/{h}")
                if event_path.exists():
                    event_device = str(event_path)
            if h.startswith("js"):
                js_path = Path(f"/dev/input/{h}")
                if js_path.exists():
                    js_device = str(js_path)

        # Look up in known controllers database
        usb_key = (vendor, product)
        known = KNOWN_CONTROLLERS.get(usb_key)

        controller = {
            "name": known["name"] if known else name,
            "vendor_id": f"{vendor:04x}",
            "product_id": f"{product:04x}",
            "usb_id": f"{vendor:04x}:{product:04x}",
            "event_device": event_device,
            "js_device": js_device,
            "known": known is not None,
            "type": known["type"] if known else "unknown",
            "era": known["era"] if known else "Modern",
            "authenticity_class": known["authenticity_class"] if known else "standard",
            "bonus_multiplier": AUTHENTICITY_BONUSES.get(
                known["authenticity_class"] if known else "standard", 1.0
            ),
        }
        controllers.append(controller)

    return controllers


# ---------------------------------------------------------------------------
# Joystick device fallback detection
# ---------------------------------------------------------------------------

def detect_js_devices() -> List[Dict]:
    """Fallback detection via /dev/input/js* devices.

    Used when /proc/bus/input/devices doesn't have enough info.
    """
    controllers = []
    input_dir = Path("/dev/input")

    if not input_dir.exists():
        return controllers

    for entry in sorted(input_dir.iterdir()):
        if not entry.name.startswith("js"):
            continue

        js_num = entry.name[2:]
        name = "Unknown Controller"

        # Read name from sysfs
        name_path = Path(f"/sys/class/input/js{js_num}/device/name")
        if name_path.exists():
            try:
                name = name_path.read_text().strip()
            except OSError:
                pass

        # Read vendor/product from uevent
        vendor = 0
        product = 0
        uevent_path = Path(f"/sys/class/input/js{js_num}/device/id")
        vendor_path = uevent_path / "vendor"
        product_path = uevent_path / "product"

        if vendor_path.exists():
            try:
                vendor = int(vendor_path.read_text().strip(), 16)
            except (OSError, ValueError):
                pass
        if product_path.exists():
            try:
                product = int(product_path.read_text().strip(), 16)
            except (OSError, ValueError):
                pass

        usb_key = (vendor, product)
        known = KNOWN_CONTROLLERS.get(usb_key)

        controller = {
            "name": known["name"] if known else name,
            "vendor_id": f"{vendor:04x}",
            "product_id": f"{product:04x}",
            "usb_id": f"{vendor:04x}:{product:04x}",
            "event_device": None,
            "js_device": str(entry),
            "known": known is not None,
            "type": known["type"] if known else "unknown",
            "era": known["era"] if known else "Modern",
            "authenticity_class": known["authenticity_class"] if known else "standard",
            "bonus_multiplier": AUTHENTICITY_BONUSES.get(
                known["authenticity_class"] if known else "standard", 1.0
            ),
        }
        controllers.append(controller)

    return controllers


# ---------------------------------------------------------------------------
# Unified detection
# ---------------------------------------------------------------------------

def detect_all_controllers() -> List[Dict]:
    """Detect all connected controllers using multiple methods.

    Deduplicates by USB ID.
    """
    seen_ids = set()
    controllers = []

    # Primary: /proc/bus/input/devices
    for ctrl in detect_event_devices():
        uid = ctrl["usb_id"]
        if uid not in seen_ids:
            seen_ids.add(uid)
            controllers.append(ctrl)

    # Fallback: /dev/input/js*
    for ctrl in detect_js_devices():
        uid = ctrl["usb_id"]
        if uid not in seen_ids:
            seen_ids.add(uid)
            controllers.append(ctrl)

    return controllers


def get_best_authenticity_bonus(controllers: List[Dict], config: Dict) -> float:
    """Calculate the best authenticity bonus from connected controllers.

    Uses the highest bonus among all connected controllers, capped
    by the max_cap from config.
    """
    if not controllers:
        return 1.0

    auth_cfg = config.get("authenticity_multipliers", {})
    max_cap = auth_cfg.get("max_cap", 1.35)

    # Get the highest bonus from connected controllers
    best_bonus = max(c.get("bonus_multiplier", 1.0) for c in controllers)

    # Cap it
    return min(best_bonus, max_cap)


def get_controller_report(controllers: List[Dict]) -> Dict:
    """Generate a controller report suitable for proof-of-play heartbeat."""
    if not controllers:
        return {
            "connected": False,
            "count": 0,
            "controllers": [],
            "best_authenticity": "none",
            "bonus_multiplier": 1.0,
        }

    best_class = "standard"
    best_bonus = 1.0
    for ctrl in controllers:
        bonus = ctrl.get("bonus_multiplier", 1.0)
        if bonus > best_bonus:
            best_bonus = bonus
            best_class = ctrl.get("authenticity_class", "standard")

    return {
        "connected": True,
        "count": len(controllers),
        "controllers": [
            {
                "name": c["name"],
                "usb_id": c["usb_id"],
                "type": c["type"],
                "era": c["era"],
                "authenticity_class": c["authenticity_class"],
            }
            for c in controllers
        ],
        "best_authenticity": best_class,
        "bonus_multiplier": best_bonus,
    }


def save_controller_state(controllers: List[Dict]) -> None:
    """Save current controller state for other daemons to read."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    report = get_controller_report(controllers)
    report["detected_at"] = time.time()
    CONTROLLER_STATE_PATH.write_text(json.dumps(report, indent=2))


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def display_controllers(controllers: List[Dict]) -> None:
    """Print detected controllers in a formatted table."""
    if not controllers:
        print("\n  No game controllers detected.\n")
        print("  Supported controllers include:")
        print("    - 8BitDo SN30 Pro / Pro+")
        print("    - iBuffalo SNES Controller")
        print("    - Raphnet N64/SNES/Genesis Adapters")
        print("    - RetroFlag SNES Controller")
        print("    - PS3/PS4/PS5 Controllers")
        print("    - Xbox 360/One/Series Controllers")
        print("    - Nintendo Switch Pro Controller")
        print()
        return

    print(f"\n  === Connected Controllers ({len(controllers)}) ===\n")

    for i, ctrl in enumerate(controllers):
        known_str = "KNOWN" if ctrl["known"] else "GENERIC"
        bonus_str = f"{ctrl['bonus_multiplier']:.2f}x" if ctrl["bonus_multiplier"] > 1.0 else "none"

        # Color coding for authenticity class
        class_str = ctrl["authenticity_class"].replace("_", " ").title()

        print(f"  Controller {i + 1}: {ctrl['name']}")
        print(f"    USB ID:      {ctrl['usb_id']}")
        print(f"    Type:        {ctrl['type']} ({ctrl['era']})")
        print(f"    Status:      {known_str}")
        print(f"    Authenticity: {class_str}")
        print(f"    Bonus:       {bonus_str}")
        if ctrl.get("js_device"):
            print(f"    JS Device:   {ctrl['js_device']}")
        if ctrl.get("event_device"):
            print(f"    Event Dev:   {ctrl['event_device']}")
        print()

    # Summary
    bonused = [c for c in controllers if c["bonus_multiplier"] > 1.0]
    if bonused:
        best = max(bonused, key=lambda c: c["bonus_multiplier"])
        print(f"  Best authenticity bonus: {best['name']} ({best['bonus_multiplier']:.2f}x)")
    else:
        print("  No authenticity bonus active (standard controllers only)")
    print()


# ---------------------------------------------------------------------------
# Watch mode (continuous monitoring)
# ---------------------------------------------------------------------------

def watch_controllers(interval: float = 5.0) -> None:
    """Continuously monitor for controller changes."""
    print("  Watching for controller changes (Ctrl+C to stop)...\n")
    last_ids = set()

    while True:
        controllers = detect_all_controllers()
        current_ids = {c["usb_id"] for c in controllers}

        if current_ids != last_ids:
            # Change detected
            added = current_ids - last_ids
            removed = last_ids - current_ids

            for uid in added:
                ctrl = next((c for c in controllers if c["usb_id"] == uid), None)
                if ctrl:
                    log.info("Controller CONNECTED: %s (%s)", ctrl["name"], uid)
            for uid in removed:
                log.info("Controller DISCONNECTED: %s", uid)

            display_controllers(controllers)
            save_controller_state(controllers)
            last_ids = current_ids

        time.sleep(interval)


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
        return {}


def main():
    parser = argparse.ArgumentParser(
        description="Sophia Edge Node -- Controller Detection"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON"
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Continuously watch for controller changes"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Output heartbeat-ready report"
    )
    args = parser.parse_args()

    config = load_config()
    controllers = detect_all_controllers()

    # Always save state for other daemons
    save_controller_state(controllers)

    if args.watch:
        try:
            watch_controllers()
        except KeyboardInterrupt:
            print("\n  Controller watch stopped.\n")
        return

    if args.report:
        report = get_controller_report(controllers)
        print(json.dumps(report, indent=2))
        return

    if args.json:
        print(json.dumps(controllers, indent=2))
        return

    display_controllers(controllers)


if __name__ == "__main__":
    main()
