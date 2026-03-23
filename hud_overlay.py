#!/usr/bin/env python3
"""
rustchain-arcade: In-Game Achievement HUD Overlay

Lightweight overlay that shows achievement notifications while gaming on RPi.
Detects RetroArch process, monitors achievement_bridge state for new unlocks,
and displays a notification via SDL2 overlay or framebuffer.

Fallback: writes notification text to a file RetroArch can pick up,
and optionally plays a short chime from /opt/rustchain-arcade/sounds/.
"""

import ctypes
import ctypes.util
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
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
log = logging.getLogger("sophia-hud")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
HUD_STATE_PATH = STATE_DIR / "hud_state.json"
NOTIFICATION_FILE = STATE_DIR / "hud_notification.txt"
SOUNDS_DIR = Path("/opt/rustchain-arcade/sounds")

# RetroArch on-screen notification file (if RetroArch reads from this)
RETROARCH_NOTIFY_PATH = Path.home() / ".config" / "retroarch" / "notification.txt"

# Achievement display duration in seconds
DISPLAY_DURATION = 5.0

# Polling interval for new achievements (seconds)
POLL_INTERVAL = 2.0

# Tier badge symbols for display
TIER_BADGES = {
    "common": "[C]",
    "uncommon": "[U]",
    "rare": "[R]",
    "ultra_rare": "[UR]",
    "legendary": "[L]",
    "mastery_bonus": "[M]",
}

# Tier colors (R, G, B) for SDL2 rendering
TIER_COLORS = {
    "common": (180, 180, 180),
    "uncommon": (30, 200, 30),
    "rare": (30, 100, 255),
    "ultra_rare": (200, 50, 200),
    "legendary": (255, 200, 30),
    "mastery_bonus": (255, 215, 0),
}


# ---------------------------------------------------------------------------
# RetroArch detection (reuse logic from proof_of_play)
# ---------------------------------------------------------------------------

def is_retroarch_running(process_names: List[str] = None) -> bool:
    """Check if RetroArch (or variant) is currently running."""
    if process_names is None:
        process_names = ["retroarch", "retroarch-core", "ra32", "ra64"]

    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return False

    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline_path = entry / "cmdline"
            cmdline_raw = cmdline_path.read_bytes()
            if not cmdline_raw:
                continue
            cmdline = cmdline_raw.decode("utf-8", errors="replace").lower()
            for pname in process_names:
                if pname.lower() in cmdline:
                    return True
        except (OSError, PermissionError):
            continue

    return False


# ---------------------------------------------------------------------------
# Achievement state monitoring
# ---------------------------------------------------------------------------

def load_hud_state() -> Dict:
    """Load HUD state tracking (last seen achievement count, etc.)."""
    if HUD_STATE_PATH.exists():
        try:
            return json.loads(HUD_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_seen_count": 0, "last_seen_ids": [], "displayed_ids": []}


def save_hud_state(state: Dict) -> None:
    """Save HUD state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HUD_STATE_PATH.write_text(json.dumps(state, indent=2))


def get_recent_achievements() -> List[Dict]:
    """Read recently reported achievements from the daily_rewards log.

    The achievement_bridge writes daily_rewards.json with claim entries.
    We monitor the claims list for new entries since our last check.
    """
    daily_path = STATE_DIR / "daily_rewards.json"
    if not daily_path.exists():
        return []

    try:
        data = json.loads(daily_path.read_text())
        return data.get("claims", [])
    except (json.JSONDecodeError, OSError):
        return []


def detect_new_achievements(hud_state: Dict) -> List[Dict]:
    """Detect achievements that haven't been displayed yet.

    Compares current claims list against previously displayed set.
    """
    claims = get_recent_achievements()
    displayed = set(hud_state.get("displayed_ids", []))

    new_achievements = []
    for i, claim in enumerate(claims):
        # Use index + timestamp as unique ID since claims may not have IDs
        claim_id = f"{claim.get('time', '')}_{i}"
        if claim_id not in displayed:
            new_achievements.append({
                "claim_id": claim_id,
                "amount": claim.get("amount", 0.0),
                "time": claim.get("time", ""),
            })

    return new_achievements


def enrich_achievement(claim: Dict) -> Dict:
    """Enrich a claim with achievement details from pending_rewards or reported state.

    Tries to find matching achievement details for display.
    """
    # Read from the pending rewards or velocity tracker for more context
    velocity_path = STATE_DIR / "velocity_tracker.json"
    reported_path = STATE_DIR / "reported.json"

    title = "Achievement Unlocked!"
    tier = "common"
    game_title = ""

    # Try to get tier info from the pending rewards log
    pending_path = STATE_DIR / "pending_rewards.jsonl"
    if pending_path.exists():
        try:
            lines = pending_path.read_text().strip().split("\n")
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    reward = json.loads(line)
                    if abs(reward.get("rtc_amount", 0) - claim.get("amount", 0)) < 0.000001:
                        title = reward.get("achievement_title", title)
                        tier = reward.get("tier", tier)
                        game_title = reward.get("game_title", game_title)
                        break
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    claim["title"] = title
    claim["tier"] = tier
    claim["game_title"] = game_title
    claim["badge"] = TIER_BADGES.get(tier, "[?]")
    return claim


# ---------------------------------------------------------------------------
# Sound playback
# ---------------------------------------------------------------------------

def play_sound(sound_name: str = "achievement_unlock.wav") -> None:
    """Play a notification sound if available.

    Tries aplay (ALSA), paplay (PulseAudio), or mpv as fallback.
    """
    sound_path = SOUNDS_DIR / sound_name
    if not sound_path.exists():
        # Try alternate locations
        alt_path = Path(__file__).parent / "sounds" / sound_name
        if alt_path.exists():
            sound_path = alt_path
        else:
            log.debug("Sound file not found: %s", sound_path)
            return

    # Try different audio players in order of preference
    players = [
        ["aplay", "-q", str(sound_path)],
        ["paplay", str(sound_path)],
        ["mpv", "--no-video", "--really-quiet", str(sound_path)],
    ]

    for cmd in players:
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.debug("Playing sound via %s", cmd[0])
            return
        except FileNotFoundError:
            continue

    log.debug("No audio player available for sound playback")


# ---------------------------------------------------------------------------
# SDL2 overlay rendering
# ---------------------------------------------------------------------------

class SDL2Overlay:
    """Simple SDL2-based overlay for achievement notifications.

    Uses ctypes to load SDL2 directly -- no Python SDL2 bindings needed.
    Falls back gracefully if SDL2 is not available.
    """

    def __init__(self):
        self.available = False
        self.sdl = None
        self.window = None
        self.renderer = None
        self._try_init()

    def _try_init(self) -> None:
        """Try to initialize SDL2 for overlay rendering."""
        lib_name = ctypes.util.find_library("SDL2")
        if lib_name is None:
            # Try common paths on RPi
            for path in ["/usr/lib/arm-linux-gnueabihf/libSDL2.so",
                         "/usr/lib/aarch64-linux-gnu/libSDL2.so",
                         "libSDL2-2.0.so.0"]:
                if os.path.exists(path):
                    lib_name = path
                    break

        if lib_name is None:
            log.debug("SDL2 not found, overlay will use text fallback")
            return

        try:
            self.sdl = ctypes.CDLL(lib_name)
            self.available = True
            log.debug("SDL2 loaded: %s", lib_name)
        except OSError as e:
            log.debug("Could not load SDL2: %s", e)

    def show_notification(self, title: str, rtc: float, tier: str,
                          game_title: str, duration: float = 5.0) -> bool:
        """Display an achievement notification overlay.

        Returns True if displayed via SDL2, False if fallback needed.
        """
        if not self.available or self.sdl is None:
            return False

        # For RPi without X11, SDL2 can use the KMS/DRM backend
        # Set environment for framebuffer/KMS rendering
        if "DISPLAY" not in os.environ:
            os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")

        try:
            # Initialize SDL2 video subsystem
            SDL_INIT_VIDEO = 0x00000020
            if self.sdl.SDL_Init(SDL_INIT_VIDEO) != 0:
                log.debug("SDL_Init failed, using text fallback")
                return False

            # Create a small overlay window
            SDL_WINDOW_BORDERLESS = 0x00000010
            SDL_WINDOW_ALWAYS_ON_TOP = 0x00008000
            SDL_WINDOW_SKIP_TASKBAR = 0x00010000

            # Position at top-right corner
            width, height = 420, 120
            x_pos = 20  # SDL_WINDOWPOS_UNDEFINED = 0x1FFF0000
            y_pos = 20

            window_flags = SDL_WINDOW_BORDERLESS
            # Try to get display size for positioning
            try:
                if "DISPLAY" in os.environ:
                    window_flags |= SDL_WINDOW_ALWAYS_ON_TOP
            except Exception:
                pass

            self.window = self.sdl.SDL_CreateWindow(
                b"Sophia HUD",
                x_pos, y_pos, width, height,
                window_flags,
            )
            if not self.window:
                self.sdl.SDL_Quit()
                return False

            # Create renderer
            self.renderer = self.sdl.SDL_CreateRenderer(self.window, -1, 0)
            if not self.renderer:
                self.sdl.SDL_DestroyWindow(self.window)
                self.sdl.SDL_Quit()
                return False

            # Draw notification background
            color = TIER_COLORS.get(tier, (180, 180, 180))

            # Dark background with tier-colored border
            self.sdl.SDL_SetRenderDrawColor(self.renderer, 20, 20, 30, 230)
            self.sdl.SDL_RenderClear(self.renderer)

            # Draw tier-colored border (top bar)
            self.sdl.SDL_SetRenderDrawColor(
                self.renderer, color[0], color[1], color[2], 255
            )
            # Top bar: fill a rect manually via lines
            for row in range(4):
                self.sdl.SDL_RenderDrawLine(
                    self.renderer, 0, row, width - 1, row
                )

            # Present the frame
            self.sdl.SDL_RenderPresent(self.renderer)

            # Hold for duration then clean up
            # Use SDL_Delay to avoid blocking other things
            self.sdl.SDL_Delay(int(duration * 1000))

            # Cleanup
            self.sdl.SDL_DestroyRenderer(self.renderer)
            self.sdl.SDL_DestroyWindow(self.window)
            self.sdl.SDL_Quit()
            self.window = None
            self.renderer = None
            return True

        except Exception as e:
            log.debug("SDL2 overlay error: %s", e)
            try:
                if self.renderer:
                    self.sdl.SDL_DestroyRenderer(self.renderer)
                if self.window:
                    self.sdl.SDL_DestroyWindow(self.window)
                self.sdl.SDL_Quit()
            except Exception:
                pass
            self.window = None
            self.renderer = None
            return False


# ---------------------------------------------------------------------------
# Text-based fallback notification
# ---------------------------------------------------------------------------

def write_text_notification(title: str, rtc: float, tier: str,
                            game_title: str, badge: str) -> None:
    """Write notification to text files as fallback.

    Writes to:
      1. ~/.rustchain-arcade/hud_notification.txt (always)
      2. RetroArch notification path (if RetroArch config dir exists)
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    notification = (
        f"{badge} {title}\n"
        f"Game: {game_title}\n"
        f"RTC Earned: {rtc:.5f}\n"
        f"Tier: {tier.upper()}\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}\n"
    )

    # Write to rustchain-arcade state dir
    NOTIFICATION_FILE.write_text(notification)
    log.info("Notification written to %s", NOTIFICATION_FILE)

    # Write to RetroArch notification path if config dir exists
    ra_config_dir = RETROARCH_NOTIFY_PATH.parent
    if ra_config_dir.exists():
        try:
            RETROARCH_NOTIFY_PATH.write_text(notification)
            log.debug("Notification written to RetroArch path")
        except OSError:
            pass


def print_terminal_notification(title: str, rtc: float, tier: str,
                                game_title: str, badge: str) -> None:
    """Print a formatted notification to the terminal/journal."""
    color = TIER_COLORS.get(tier, (180, 180, 180))
    # ANSI color approximation from RGB
    if tier == "legendary":
        ansi = "\033[1;33m"  # Bold yellow
    elif tier == "ultra_rare":
        ansi = "\033[1;35m"  # Bold magenta
    elif tier == "rare":
        ansi = "\033[1;34m"  # Bold blue
    elif tier == "uncommon":
        ansi = "\033[1;32m"  # Bold green
    else:
        ansi = "\033[1;37m"  # Bold white
    reset = "\033[0m"

    box = (
        f"\n{ansi}"
        f"  +{'=' * 44}+\n"
        f"  |  {badge} ACHIEVEMENT UNLOCKED!{' ' * (21 - len(badge))}|\n"
        f"  |  {title[:40]:<40s}  |\n"
        f"  |  Game: {game_title[:36]:<36s}  |\n"
        f"  |  RTC: {rtc:<12.5f} Tier: {tier.upper():<12s}  |\n"
        f"  +{'=' * 44}+\n"
        f"{reset}"
    )
    print(box, flush=True)


# ---------------------------------------------------------------------------
# Notification display orchestrator
# ---------------------------------------------------------------------------

def display_notification(achievement: Dict, config: Dict,
                         sdl_overlay: Optional[SDL2Overlay] = None) -> None:
    """Display an achievement notification through all available channels.

    Priority order:
      1. SDL2 overlay (if available and RetroArch running)
      2. Text file fallback (always)
      3. Terminal output (always)
      4. Sound effect (if sounds dir exists)
    """
    title = achievement.get("title", "Achievement Unlocked!")
    rtc = achievement.get("amount", 0.0)
    tier = achievement.get("tier", "common")
    game_title = achievement.get("game_title", "")
    badge = achievement.get("badge", "[?]")

    # Always write text fallback
    write_text_notification(title, rtc, tier, game_title, badge)

    # Always print to terminal/journal
    print_terminal_notification(title, rtc, tier, game_title, badge)

    # Try SDL2 overlay
    sdl_displayed = False
    if sdl_overlay is not None:
        sdl_displayed = sdl_overlay.show_notification(
            title, rtc, tier, game_title, duration=DISPLAY_DURATION
        )

    # Play sound
    sound_enabled = config.get("hud", {}).get("sound_enabled", True)
    if sound_enabled:
        if tier in ("legendary", "ultra_rare"):
            play_sound("mastery.wav")
        else:
            play_sound("achievement_unlock.wav")

    log.info("Notification displayed: %s (%.5f RTC, %s)%s",
             title, rtc, tier, " [SDL2]" if sdl_displayed else " [text]")


# ---------------------------------------------------------------------------
# Main HUD loop
# ---------------------------------------------------------------------------

def hud_loop(config: Dict) -> None:
    """Main loop: watch for new achievements and display notifications."""
    process_names = config.get("proof_of_play", {}).get(
        "retroarch_process_names", ["retroarch", "retroarch-core", "ra32", "ra64"]
    )

    log.info("=== Sophia Achievement HUD Overlay ===")
    log.info("Poll interval: %.1fs", POLL_INTERVAL)
    log.info("Display duration: %.1fs", DISPLAY_DURATION)
    log.info("Sounds dir: %s (exists: %s)", SOUNDS_DIR, SOUNDS_DIR.exists())

    # Try to initialize SDL2 overlay
    sdl_overlay = SDL2Overlay()
    if sdl_overlay.available:
        log.info("SDL2 overlay: available")
    else:
        log.info("SDL2 overlay: not available, using text fallback")

    hud_state = load_hud_state()

    while True:
        try:
            # Only show notifications when RetroArch is running
            if not is_retroarch_running(process_names):
                time.sleep(POLL_INTERVAL * 5)  # Slow poll when not gaming
                continue

            # Check for new achievements
            new_achievements = detect_new_achievements(hud_state)

            for ach in new_achievements:
                # Enrich with details
                ach = enrich_achievement(ach)

                # Display notification
                display_notification(ach, config, sdl_overlay)

                # Mark as displayed
                displayed = hud_state.get("displayed_ids", [])
                displayed.append(ach["claim_id"])
                # Keep only last 500 IDs to prevent unbounded growth
                if len(displayed) > 500:
                    displayed = displayed[-500:]
                hud_state["displayed_ids"] = displayed
                save_hud_state(hud_state)

                # Brief pause between multiple notifications
                if len(new_achievements) > 1:
                    time.sleep(1.0)

        except Exception:
            log.exception("Error in HUD loop")

        time.sleep(POLL_INTERVAL)


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
        log.warning("Config not found at %s, using defaults", cfg_path)
        return {}


def main():
    config = load_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    try:
        hud_loop(config)
    except KeyboardInterrupt:
        log.info("HUD overlay stopped by user.")


if __name__ == "__main__":
    main()
