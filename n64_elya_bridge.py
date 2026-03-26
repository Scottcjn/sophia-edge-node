#!/usr/bin/env python3
"""
rustchain-arcade: N64 Legend of Elya Achievement Bridge

Bridges RetroArch running Legend of Elya (N64 homebrew) with the RustChain
attestation system for custom RTC achievement rewards.

How it works:
  1. RPi (or any host) runs RetroArch with mupen64plus-next or parallel-n64 core
  2. Bridge reads N64 RDRAM via RetroArch Network Command Interface (UDP 55355)
  3. Monitors GameCtx struct to detect in-game achievements
  4. Awards RTC via the existing rustchain-arcade achievement/proof-of-play system

N64 RDRAM memory map monitored (from legend_of_elya.c GameCtx struct):
  - current_room  : which room the player is in (0=dungeon, 1=library, 2=forge)
  - state         : game state (dialog, keyboard, generating, etc)
  - current_npc   : which NPC is being talked to (-1/0/1/2)
  - dialog_done   : incremented when a dialog completes
  - dialog_len    : current dialog buffer length (proxy for token count)
  - frame         : total frame counter (session activity)

Achievement multiplier: N64 gets 1.5x antiquity via emulation, 3.0x on real N64.

Usage:
  python3 n64_elya_bridge.py                         # Auto-detect everything
  python3 n64_elya_bridge.py --addr 0x100000         # Specify GameCtx address
  python3 n64_elya_bridge.py --real-n64              # Real N64 hardware (3.0x)
  python3 n64_elya_bridge.py --dry-run               # Preview without RTC claims
"""

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import requests

# ---------------------------------------------------------------------------
# Sibling imports from rustchain-arcade
# ---------------------------------------------------------------------------
# Add parent dir to path for imports when run from this directory
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from n64_memory_reader import (
    N64GameState,
    RetroArchMemoryReader,
    detect_game_ctx_address,
    ROOM_DUNGEON,
    ROOM_LIBRARY,
    ROOM_FORGE,
    ROOM_NAMES,
    STATE_DIALOG,
    STATE_DIALOG_SELECT,
    STATE_GENERATING,
    STATE_KEYBOARD,
    STATE_DUNGEON,
)
from proof_of_play import (
    load_current_session,
    save_current_session,
    calculate_boost_multiplier,
    create_heartbeat,
    submit_heartbeat,
    submit_proof_of_play,
    archive_session,
    STATE_DIR,
    SESSIONS_DIR,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("n64-elya-bridge")

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)

N64_STATE_DIR = STATE_DIR / "n64_elya"
ACHIEVEMENTS_PATH = N64_STATE_DIR / "achievements.json"
SESSION_LOG_PATH = N64_STATE_DIR / "session_log.jsonl"
PENDING_REWARDS_PATH = STATE_DIR / "pending_rewards.jsonl"

# Legend of Elya game identity
ELYA_GAME_ID = "legend-of-elya-n64"
ELYA_GAME_TITLE = "Legend of Elya"
ELYA_PLATFORM = "nintendo 64"
ELYA_ROM_HASH_PREFIX = "legend_of_elya"  # Matches ROM hash check

# Antiquity multipliers for N64
# Emulated N64 (via RetroArch on RPi/PC): 1996 hardware via emulation = 1.5x
# Real N64 via UltraHDMI/Everdrive + bridge: full vintage = 3.0x
N64_EMULATED_MULTIPLIER = 1.5
N64_REAL_HARDWARE_MULTIPLIER = 3.0

# ---------------------------------------------------------------------------
# Achievement definitions
# ---------------------------------------------------------------------------

@dataclass
class Achievement:
    """Definition of an N64 Legend of Elya achievement."""
    achievement_id: str
    title: str
    description: str
    rtc_reward: float
    category: str = "exploration"
    soulbound_relic: bool = False  # If True, mints a Cartridge Relic on unlock

    def to_dict(self) -> Dict:
        return {
            "achievement_id": self.achievement_id,
            "title": self.title,
            "description": self.description,
            "rtc_reward": self.rtc_reward,
            "category": self.category,
            "soulbound_relic": self.soulbound_relic,
        }


# All 10 Legend of Elya achievements
ACHIEVEMENTS: Dict[str, Achievement] = {
    "first_contact": Achievement(
        achievement_id="elya_first_contact",
        title="First Contact",
        description="Talk to Sophia Elya for the first time",
        rtc_reward=1.0,
        category="story",
    ),
    "scholars_path": Achievement(
        achievement_id="elya_scholars_path",
        title="Scholar's Path",
        description="Visit the Arcane Library",
        rtc_reward=0.5,
        category="exploration",
    ),
    "forge_born": Achievement(
        achievement_id="elya_forge_born",
        title="Forge Born",
        description="Visit the Ember Forge",
        rtc_reward=0.5,
        category="exploration",
    ),
    "explorer": Achievement(
        achievement_id="elya_explorer",
        title="Explorer",
        description="Visit all 3 rooms in one session",
        rtc_reward=2.0,
        category="exploration",
    ),
    "custom_prompt": Achievement(
        achievement_id="elya_custom_prompt",
        title="Custom Prompt",
        description="Use the virtual keyboard to type a custom prompt",
        rtc_reward=1.5,
        category="interaction",
    ),
    "polyglot": Achievement(
        achievement_id="elya_polyglot",
        title="Polyglot",
        description="Talk to all 3 NPCs in one session",
        rtc_reward=3.0,
        category="social",
    ),
    "deep_thinker": Achievement(
        achievement_id="elya_deep_thinker",
        title="Deep Thinker",
        description="Generate 100+ tokens in total",
        rtc_reward=1.0,
        category="interaction",
    ),
    "philosopher": Achievement(
        achievement_id="elya_philosopher",
        title="Philosopher",
        description="Generate 500+ tokens in total",
        rtc_reward=2.0,
        category="interaction",
    ),
    "sages_apprentice": Achievement(
        achievement_id="elya_sages_apprentice",
        title="The Sage's Apprentice",
        description="Complete 10 dialogs with Sophia Elya",
        rtc_reward=5.0,
        category="story",
    ),
    "master_of_elya": Achievement(
        achievement_id="elya_master_of_elya",
        title="Master of Elya",
        description="Unlock all other achievements",
        rtc_reward=10.0,
        category="mastery",
        soulbound_relic=True,
    ),
}

# Which achievements are prerequisites for "Master of Elya"
MASTER_PREREQUISITES = {
    k for k in ACHIEVEMENTS if k != "master_of_elya"
}


# ---------------------------------------------------------------------------
# Achievement state tracking
# ---------------------------------------------------------------------------

@dataclass
class ElyaSessionState:
    """Track in-session progress for achievement detection."""
    session_id: str = ""
    started_at: float = 0.0
    # Room visits
    rooms_visited: Set[int] = field(default_factory=set)
    # NPC interactions
    npcs_talked_to: Set[int] = field(default_factory=set)
    sophia_dialog_count: int = 0
    # Token generation tracking
    total_tokens_estimated: int = 0
    total_dialogs_completed: int = 0
    # Keyboard usage
    keyboard_used: bool = False
    # Previous frame state for edge detection
    prev_state: int = -1
    prev_dialog_done: int = 0
    prev_current_room: int = -1
    prev_current_npc: int = -1
    prev_dialog_len: int = 0
    # Achievements unlocked this session
    unlocked_this_session: Set[str] = field(default_factory=set)
    # All-time unlocked achievements (loaded from disk)
    unlocked_all_time: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "rooms_visited": sorted(self.rooms_visited),
            "npcs_talked_to": sorted(self.npcs_talked_to),
            "sophia_dialog_count": self.sophia_dialog_count,
            "total_tokens_estimated": self.total_tokens_estimated,
            "total_dialogs_completed": self.total_dialogs_completed,
            "keyboard_used": self.keyboard_used,
            "unlocked_this_session": sorted(self.unlocked_this_session),
            "unlocked_all_time": sorted(self.unlocked_all_time),
        }


def load_achievements() -> Dict:
    """Load previously unlocked achievements from disk."""
    N64_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if ACHIEVEMENTS_PATH.exists():
        try:
            return json.loads(ACHIEVEMENTS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"unlocked": [], "total_rtc_earned": 0.0, "unlock_history": []}


def save_achievements(data: Dict) -> None:
    """Persist achievement state."""
    N64_STATE_DIR.mkdir(parents=True, exist_ok=True)
    ACHIEVEMENTS_PATH.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Achievement detection logic
# ---------------------------------------------------------------------------

# Average tokens per dialog in Legend of Elya (128-byte buffer, ~80 useful chars)
AVG_TOKENS_PER_DIALOG = 60


def check_achievements(
    gs: N64GameState,
    session: ElyaSessionState,
) -> List[str]:
    """Detect newly unlocked achievements by comparing current RDRAM state
    to session tracking state.

    Returns list of achievement keys that were just unlocked.
    """
    newly_unlocked: List[str] = []

    # --- Edge detection: state transitions ---

    # Track room visits
    if gs.current_room in (ROOM_DUNGEON, ROOM_LIBRARY, ROOM_FORGE):
        if gs.current_room not in session.rooms_visited:
            session.rooms_visited.add(gs.current_room)
            log.info("  Room visited: %s", ROOM_NAMES.get(gs.current_room, "?"))

    # Detect dialog completion (dialog_done transitions from 0 -> 1)
    # In Legend of Elya, dialog_done is set when generation finishes
    if gs.dialog_done and not session.prev_dialog_done:
        session.total_dialogs_completed += 1
        # Estimate tokens from dialog length
        tokens_this_dialog = max(gs.dialog_len, AVG_TOKENS_PER_DIALOG)
        session.total_tokens_estimated += tokens_this_dialog
        log.info("  Dialog completed (#%d, ~%d tokens, total ~%d tokens)",
                 session.total_dialogs_completed,
                 tokens_this_dialog,
                 session.total_tokens_estimated)

        # Track NPC interactions
        if gs.current_npc >= 0:
            if gs.current_npc not in session.npcs_talked_to:
                session.npcs_talked_to.add(gs.current_npc)
                npc_names = {0: "Sophia Elya", 1: "Aldric the Keeper", 2: "Brunhild"}
                log.info("  First conversation with %s",
                         npc_names.get(gs.current_npc, f"NPC {gs.current_npc}"))

            # Count Sophia-specific dialogs
            if gs.current_npc == 0:
                session.sophia_dialog_count += 1

    # Detect keyboard usage (state transitions to STATE_KEYBOARD)
    if gs.state == STATE_KEYBOARD and session.prev_state != STATE_KEYBOARD:
        session.keyboard_used = True
        log.info("  Virtual keyboard opened")

    # --- Check each achievement ---

    def _try_unlock(key: str) -> bool:
        """Try to unlock an achievement. Returns True if newly unlocked."""
        if key in session.unlocked_all_time:
            return False
        if key in session.unlocked_this_session:
            return False
        session.unlocked_this_session.add(key)
        session.unlocked_all_time.add(key)
        newly_unlocked.append(key)
        return True

    # 1. First Contact — Talk to Sophia (NPC 0) for the first time
    if 0 in session.npcs_talked_to:
        _try_unlock("first_contact")

    # 2. Scholar's Path — Visit the Arcane Library
    if ROOM_LIBRARY in session.rooms_visited:
        _try_unlock("scholars_path")

    # 3. Forge Born — Visit the Ember Forge
    if ROOM_FORGE in session.rooms_visited:
        _try_unlock("forge_born")

    # 4. Explorer — Visit all 3 rooms in one session
    if len(session.rooms_visited) >= 3:
        _try_unlock("explorer")

    # 5. Custom Prompt — Use the virtual keyboard
    if session.keyboard_used:
        _try_unlock("custom_prompt")

    # 6. Polyglot — Talk to all 3 NPCs
    if len(session.npcs_talked_to) >= 3:
        _try_unlock("polyglot")

    # 7. Deep Thinker — Generate 100+ tokens
    if session.total_tokens_estimated >= 100:
        _try_unlock("deep_thinker")

    # 8. Philosopher — Generate 500+ tokens
    if session.total_tokens_estimated >= 500:
        _try_unlock("philosopher")

    # 9. The Sage's Apprentice — 10 dialogs with Sophia
    if session.sophia_dialog_count >= 10:
        _try_unlock("sages_apprentice")

    # 10. Master of Elya — All other achievements unlocked
    if MASTER_PREREQUISITES.issubset(session.unlocked_all_time):
        _try_unlock("master_of_elya")

    # Update edge-detection state for next poll
    session.prev_state = gs.state
    session.prev_dialog_done = gs.dialog_done
    session.prev_current_room = gs.current_room
    session.prev_current_npc = gs.current_npc
    session.prev_dialog_len = gs.dialog_len

    return newly_unlocked


# ---------------------------------------------------------------------------
# RTC reward submission
# ---------------------------------------------------------------------------

def submit_n64_achievement(
    config: Dict,
    achievement: Achievement,
    session: ElyaSessionState,
    antiquity_multiplier: float,
    session_boost: float,
    dry_run: bool = False,
) -> bool:
    """Submit an N64 Legend of Elya achievement reward to the RustChain node.

    Uses the same /api/gaming/achievement endpoint as achievement_bridge.py
    but with source="n64_legend_of_elya" and N64 antiquity multiplier.
    """
    node_url = config["rustchain"]["node_url"].rstrip("/")
    verify_ssl = config["rustchain"].get("verify_ssl", False)
    wallet_id = os.environ.get(
        "SOPHIA_WALLET", config.get("node_id", "rustchain-arcade-rpi")
    )

    # Final RTC = base_reward * antiquity * session_boost
    final_rtc = achievement.rtc_reward * antiquity_multiplier * session_boost

    payload = {
        "miner": wallet_id,
        "source": "n64_legend_of_elya",
        "achievement_id": achievement.achievement_id,
        "game_id": ELYA_GAME_ID,
        "game_title": ELYA_GAME_TITLE,
        "achievement_title": achievement.title,
        "points": int(achievement.rtc_reward * 10),  # Map to point scale
        "tier": achievement.category,
        "rtc_amount": round(final_rtc, 6),
        "hardcore": False,  # N64 homebrew, not RetroAchievements
        "rarity_factor": 1.0,
        "session_boost": session_boost,
        "antiquity_multiplier": antiquity_multiplier,
        "n64_platform": True,
        "session_id": session.session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if dry_run:
        log.info("  [DRY RUN] Would submit: %s = %.4f RTC (base %.1f * ant %.1fx * boost %.1fx)",
                 achievement.title, final_rtc,
                 achievement.rtc_reward, antiquity_multiplier, session_boost)
        return True

    url = f"{node_url}/api/gaming/achievement"
    try:
        resp = requests.post(url, json=payload, verify=verify_ssl, timeout=15)
        if resp.status_code == 200:
            log.info("  Achievement reward submitted: %s = %.4f RTC",
                     achievement.title, final_rtc)
            return True
        else:
            log.warning("  Node rejected reward (HTTP %d): %s",
                        resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("  Could not reach node: %s", e)

    # Store locally for batch submission later
    try:
        with open(PENDING_REWARDS_PATH, "a") as f:
            f.write(json.dumps(payload) + "\n")
        log.info("  Stored reward locally for batch submission (%.4f RTC)", final_rtc)
    except OSError as e:
        log.error("  Failed to store pending reward: %s", e)

    return False


def mint_master_relic(
    config: Dict,
    session: ElyaSessionState,
    total_rtc: float,
) -> None:
    """Mint a soulbound Cartridge Relic for Master of Elya achievement.

    Uses the CartridgeWallet from cartridge_wallet.py.
    """
    try:
        from cartridge_wallet import CartridgeWallet
        cw = CartridgeWallet()
        cart = cw.mint_cartridge(
            game_id=hash(ELYA_GAME_ID) & 0x7FFFFFFF,
            game_title=ELYA_GAME_TITLE,
            platform=ELYA_PLATFORM,
            achievement_count=len(ACHIEVEMENTS),
            hardcore=False,
            total_rtc_earned=total_rtc,
            rarity_badges=["N64", "LLM", "HOMEBREW"],
            first_press=False,  # Let the node decide
        )
        log.info("  Cartridge Relic minted: %s", cart.get("cartridge_id", "?"))
        if cart.get("ascii_art"):
            for line in cart["ascii_art"].split("\n"):
                log.info("  %s", line)
    except ImportError:
        log.warning("  cartridge_wallet.py not available, skipping relic mint")
    except Exception as e:
        log.error("  Failed to mint Cartridge Relic: %s", e)


# ---------------------------------------------------------------------------
# Main bridge loop
# ---------------------------------------------------------------------------

def load_config() -> Dict:
    """Load config.json."""
    cfg_path = Path(CONFIG_PATH)
    # Try local directory first, then installed path
    if not cfg_path.exists():
        local_cfg = _SCRIPT_DIR / "config.json"
        if local_cfg.exists():
            cfg_path = local_cfg
    if cfg_path.exists():
        with open(cfg_path) as f:
            return json.load(f)
    else:
        log.error("Config not found at %s", cfg_path)
        sys.exit(1)


def bridge_loop(
    config: Dict,
    reader: RetroArchMemoryReader,
    real_n64: bool = False,
    dry_run: bool = False,
    poll_hz: float = 4.0,
) -> None:
    """Main loop: poll RDRAM, detect achievements, submit rewards.

    Args:
        config: rustchain-arcade config dict
        reader: connected RetroArchMemoryReader with game_ctx_base set
        real_n64: if True, use 3.0x antiquity multiplier (real N64 hardware)
        dry_run: if True, print rewards but don't submit
        poll_hz: how many times per second to poll RDRAM (default 4)
    """
    antiquity_mult = N64_REAL_HARDWARE_MULTIPLIER if real_n64 else N64_EMULATED_MULTIPLIER
    poll_interval = 1.0 / poll_hz

    # Load all-time achievement state
    ach_data = load_achievements()
    all_time_unlocked = set(ach_data.get("unlocked", []))
    total_rtc = ach_data.get("total_rtc_earned", 0.0)

    # Create session
    now = time.time()
    session_id = hashlib.sha256(
        f"{now}:{ELYA_GAME_ID}:{os.getpid()}".encode()
    ).hexdigest()[:16]

    session = ElyaSessionState(
        session_id=session_id,
        started_at=now,
        unlocked_all_time=all_time_unlocked,
    )

    hw_mode = "REAL N64" if real_n64 else "EMULATED"
    log.info("=== N64 Legend of Elya Achievement Bridge ===")
    log.info("Session: %s", session_id)
    log.info("Hardware: %s (%.1fx antiquity)", hw_mode, antiquity_mult)
    log.info("GameCtx base: 0x%06x", reader.game_ctx_base)
    log.info("Poll rate: %.1f Hz", poll_hz)
    log.info("Achievements already unlocked: %d/%d",
             len(all_time_unlocked), len(ACHIEVEMENTS))
    if dry_run:
        log.info("DRY RUN MODE — no RTC will be submitted")
    log.info("")

    # Session boost config
    boost_config = config.get("proof_of_play", {}).get(
        "session_boost_multipliers", {}
    )

    consecutive_failures = 0
    last_heartbeat = 0.0
    heartbeat_interval = config.get("proof_of_play", {}).get(
        "heartbeat_interval_seconds", 60
    )

    while True:
        try:
            gs = reader.read_game_state()

            if not gs.read_ok:
                consecutive_failures += 1
                if consecutive_failures >= 20:
                    log.warning("Lost connection to RetroArch (%d failures). "
                                "Waiting for reconnect...",
                                consecutive_failures)
                    time.sleep(5)
                    if reader.connect():
                        consecutive_failures = 0
                    continue
                time.sleep(poll_interval)
                continue

            consecutive_failures = 0

            # Only process when game is active (past title screen)
            if not gs.is_playing:
                time.sleep(poll_interval)
                continue

            # --- Check achievements ---
            newly_unlocked = check_achievements(gs, session)

            for key in newly_unlocked:
                ach = ACHIEVEMENTS[key]
                # Calculate session boost
                duration_s = time.time() - session.started_at
                session_boost = calculate_boost_multiplier(
                    duration_s,
                    len(session.unlocked_this_session),
                    key == "master_of_elya",
                    boost_config,
                )

                final_rtc = ach.rtc_reward * antiquity_mult * session_boost

                log.info("")
                log.info("  *** ACHIEVEMENT UNLOCKED: %s ***", ach.title)
                log.info("  %s", ach.description)
                log.info("  Reward: %.4f RTC (base %.1f * ant %.1fx * boost %.1fx)",
                         final_rtc, ach.rtc_reward, antiquity_mult, session_boost)

                # Submit reward
                submit_n64_achievement(
                    config, ach, session, antiquity_mult, session_boost, dry_run,
                )

                # Update totals
                total_rtc += final_rtc

                # Mint Cartridge Relic for Master of Elya
                if key == "master_of_elya" and ach.soulbound_relic and not dry_run:
                    mint_master_relic(config, session, total_rtc)

                # Persist achievement state
                ach_data["unlocked"] = sorted(session.unlocked_all_time)
                ach_data["total_rtc_earned"] = round(total_rtc, 6)
                ach_data["unlock_history"].append({
                    "key": key,
                    "achievement_id": ach.achievement_id,
                    "title": ach.title,
                    "rtc": round(final_rtc, 6),
                    "session_id": session.session_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                save_achievements(ach_data)

                log.info("  Progress: %d/%d achievements, %.4f RTC total",
                         len(session.unlocked_all_time), len(ACHIEVEMENTS),
                         total_rtc)
                log.info("")

            # --- Periodic heartbeat for Proof of Play ---
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                pop_session = load_current_session()
                if pop_session and pop_session.get("active"):
                    pop_session["achievements_earned"] = len(
                        session.unlocked_this_session
                    )
                    heartbeat = create_heartbeat(pop_session, config)
                    heartbeat["n64_bridge"] = True
                    heartbeat["n64_room"] = gs.room_name
                    heartbeat["n64_state"] = gs.state_name
                    heartbeat["n64_dialogs"] = session.total_dialogs_completed
                    heartbeat["n64_tokens_est"] = session.total_tokens_estimated
                    if not dry_run:
                        submit_heartbeat(heartbeat, config)
                last_heartbeat = now

        except KeyboardInterrupt:
            break
        except Exception:
            log.exception("Error in bridge loop")
            time.sleep(1)
            continue

        time.sleep(poll_interval)

    # --- Session end ---
    log.info("")
    log.info("=== Session Summary ===")
    log.info("Duration: %.1f minutes", (time.time() - session.started_at) / 60.0)
    log.info("Rooms visited: %s",
             ", ".join(ROOM_NAMES.get(r, "?") for r in sorted(session.rooms_visited)))
    log.info("NPCs talked to: %d", len(session.npcs_talked_to))
    log.info("Dialogs completed: %d", session.total_dialogs_completed)
    log.info("Tokens estimated: %d", session.total_tokens_estimated)
    log.info("Achievements this session: %d", len(session.unlocked_this_session))
    log.info("Total achievements: %d/%d", len(session.unlocked_all_time), len(ACHIEVEMENTS))
    log.info("Total RTC earned: %.4f", total_rtc)

    # Log session
    try:
        N64_STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SESSION_LOG_PATH, "a") as f:
            f.write(json.dumps(session.to_dict(), default=str) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="N64 Legend of Elya Achievement Bridge for RustChain",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="RetroArch host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=55355,
        help="RetroArch UDP command port (default: 55355)",
    )
    parser.add_argument(
        "--addr", default=None,
        help="GameCtx RDRAM address in hex (e.g., 0x100000). "
             "Auto-detected if not specified.",
    )
    parser.add_argument(
        "--real-n64", action="store_true",
        help="Real N64 hardware mode (3.0x antiquity instead of 1.5x)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview achievements without submitting RTC claims",
    )
    parser.add_argument(
        "--poll-hz", type=float, default=4.0,
        help="RDRAM poll rate in Hz (default: 4.0)",
    )
    parser.add_argument(
        "--list-achievements", action="store_true",
        help="List all achievements and exit",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Show current achievement progress and exit",
    )
    args = parser.parse_args()

    # --- List achievements ---
    if args.list_achievements:
        print("\n  Legend of Elya -- N64 Achievement List")
        print("  " + "=" * 50)
        for key, ach in ACHIEVEMENTS.items():
            relic = " [SOULBOUND RELIC]" if ach.soulbound_relic else ""
            print(f"  {ach.rtc_reward:5.1f} RTC  {ach.title}{relic}")
            print(f"           {ach.description}")
        total = sum(a.rtc_reward for a in ACHIEVEMENTS.values())
        print(f"\n  Total possible: {total:.1f} RTC (before multipliers)")
        print(f"  N64 emulated: {total * N64_EMULATED_MULTIPLIER:.1f} RTC (1.5x)")
        print(f"  Real N64:     {total * N64_REAL_HARDWARE_MULTIPLIER:.1f} RTC (3.0x)")
        print()
        return

    # --- Show status ---
    if args.status:
        ach_data = load_achievements()
        unlocked = set(ach_data.get("unlocked", []))
        total_rtc = ach_data.get("total_rtc_earned", 0.0)
        print(f"\n  Legend of Elya -- Achievement Progress")
        print("  " + "=" * 50)
        for key, ach in ACHIEVEMENTS.items():
            mark = "[x]" if key in unlocked else "[ ]"
            print(f"  {mark} {ach.title:25s} ({ach.rtc_reward:.1f} RTC)")
        print(f"\n  Unlocked: {len(unlocked)}/{len(ACHIEVEMENTS)}")
        print(f"  Total RTC earned: {total_rtc:.4f}")
        history = ach_data.get("unlock_history", [])
        if history:
            print(f"\n  Recent unlocks:")
            for entry in history[-5:]:
                print(f"    {entry.get('timestamp', '?')[:19]}  "
                      f"{entry.get('title', '?')} ({entry.get('rtc', 0):.4f} RTC)")
        print()
        return

    # --- Main bridge ---
    config = load_config()

    reader = RetroArchMemoryReader(host=args.host, port=args.port)
    log.info("Connecting to RetroArch at %s:%d...", args.host, args.port)

    if not reader.connect():
        log.error("Could not connect to RetroArch. Is it running?")
        log.error("  Start RetroArch with: retroarch --cmd-port 55355")
        log.error("  Or set network_cmd_enable = true in retroarch.cfg")
        sys.exit(1)

    # Determine GameCtx address
    if args.addr:
        reader.game_ctx_base = int(args.addr, 0)
        log.info("Using specified GameCtx address: 0x%06x", reader.game_ctx_base)
    else:
        log.info("Auto-detecting GameCtx address in RDRAM...")
        addr = detect_game_ctx_address(reader)
        if addr is None:
            log.error("Could not find GameCtx in RDRAM. Try specifying --addr.")
            log.error("  To find it: nm legend_of_elya.elf | grep ' G$'")
            sys.exit(1)
        reader.game_ctx_base = addr

    # Verify we can read valid game state
    test_gs = reader.read_game_state()
    if not test_gs.read_ok:
        log.error("Failed to read game state. Is Legend of Elya loaded in RetroArch?")
        sys.exit(1)

    log.info("Game state read OK: state=%s room=%s frame=%d",
             test_gs.state_name, test_gs.room_name, test_gs.frame)

    try:
        bridge_loop(
            config=config,
            reader=reader,
            real_n64=args.real_n64,
            dry_run=args.dry_run,
            poll_hz=args.poll_hz,
        )
    except KeyboardInterrupt:
        log.info("Bridge stopped by user.")
    finally:
        reader.close()


if __name__ == "__main__":
    main()
