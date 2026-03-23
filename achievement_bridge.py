#!/usr/bin/env python3
"""
rustchain-arcade: RetroAchievements -> RTC Reward Bridge

Polls RetroAchievements.org for recently unlocked achievements, classifies
them by point value into reward tiers, and submits RTC reward claims to the
RustChain network.

Features:
  - Rarity-weighted scoring via RA unlock percentages
  - Proof of Play session boost integration
  - Mastery milestone system (first_clear, full_mastery, legendary, system_crown)
  - Achievement velocity anti-cheat (>20/hr flagged)
  - Common/uncommon tier throttling (8/game/day half-pay)
  - Cartridge wallet integration for mastered games
  - Victory Lap detection (mastery -> 5x next epoch)
  - Offline pending_rewards.jsonl batching
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sophia-achievements")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
REPORTED_PATH = STATE_DIR / "reported.json"
DAILY_LOG_PATH = STATE_DIR / "daily_rewards.json"
PENDING_REWARDS_PATH = STATE_DIR / "pending_rewards.jsonl"
VELOCITY_PATH = STATE_DIR / "velocity_tracker.json"
TIER_THROTTLE_PATH = STATE_DIR / "tier_throttle.json"
VICTORY_LAP_PATH = STATE_DIR / "victory_lap.json"
CARTRIDGE_DIR = STATE_DIR / "cartridges"

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_reported() -> Dict:
    """Load set of already-reported achievement IDs and mastered game IDs."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if REPORTED_PATH.exists():
        try:
            return json.loads(REPORTED_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"achievements": [], "mastered_games": []}


def save_reported(data: Dict) -> None:
    """Persist reported achievement/mastery state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTED_PATH.write_text(json.dumps(data, indent=2))


def get_daily_spent(wallet_cap: float = 0.10) -> float:
    """Return total RTC claimed today."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if DAILY_LOG_PATH.exists():
        try:
            daily = json.loads(DAILY_LOG_PATH.read_text())
            if daily.get("date") == today:
                return daily.get("total_rtc", 0.0)
        except (json.JSONDecodeError, OSError):
            pass
    return 0.0


def add_daily_spent(amount: float) -> float:
    """Record additional RTC spent today. Returns new total."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    daily = {"date": today, "total_rtc": 0.0, "claims": []}
    if DAILY_LOG_PATH.exists():
        try:
            loaded = json.loads(DAILY_LOG_PATH.read_text())
            if loaded.get("date") == today:
                daily = loaded
        except (json.JSONDecodeError, OSError):
            pass

    daily["date"] = today
    daily["total_rtc"] = daily.get("total_rtc", 0.0) + amount
    daily["claims"].append({
        "amount": amount,
        "time": datetime.now(timezone.utc).isoformat(),
    })
    DAILY_LOG_PATH.write_text(json.dumps(daily, indent=2))
    return daily["total_rtc"]


# ---------------------------------------------------------------------------
# Achievement velocity anti-cheat
# ---------------------------------------------------------------------------

def load_velocity_tracker() -> Dict:
    """Load achievement velocity data for anti-cheat."""
    if VELOCITY_PATH.exists():
        try:
            return json.loads(VELOCITY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"timestamps": [], "flagged": False}


def save_velocity_tracker(data: Dict) -> None:
    """Save velocity tracker state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    VELOCITY_PATH.write_text(json.dumps(data, indent=2))


def check_achievement_velocity(max_per_hour: int = 20) -> Tuple[bool, int]:
    """Check if achievement unlock rate exceeds anti-cheat threshold.

    Returns (is_ok, count_this_hour).
    Prunes timestamps older than 1 hour.
    """
    tracker = load_velocity_tracker()
    now = time.time()
    one_hour_ago = now - 3600

    # Prune old timestamps
    recent = [ts for ts in tracker.get("timestamps", []) if ts > one_hour_ago]
    count = len(recent)

    if count >= max_per_hour:
        tracker["flagged"] = True
        tracker["flagged_at"] = datetime.now(timezone.utc).isoformat()
        tracker["timestamps"] = recent
        save_velocity_tracker(tracker)
        return False, count

    tracker["timestamps"] = recent
    tracker["flagged"] = False
    save_velocity_tracker(tracker)
    return True, count


def record_achievement_timestamp() -> None:
    """Record a new achievement unlock timestamp for velocity tracking."""
    tracker = load_velocity_tracker()
    now = time.time()
    one_hour_ago = now - 3600
    recent = [ts for ts in tracker.get("timestamps", []) if ts > one_hour_ago]
    recent.append(now)
    tracker["timestamps"] = recent
    save_velocity_tracker(tracker)


# ---------------------------------------------------------------------------
# Common/uncommon tier throttling
# ---------------------------------------------------------------------------

def load_tier_throttle() -> Dict:
    """Load per-game tier throttle data."""
    if TIER_THROTTLE_PATH.exists():
        try:
            data = json.loads(TIER_THROTTLE_PATH.read_text())
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "games": {}}


def save_tier_throttle(data: Dict) -> None:
    """Save tier throttle state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TIER_THROTTLE_PATH.write_text(json.dumps(data, indent=2))


def check_tier_throttle(game_id: str, tier_name: str, throttle_limit: int = 8) -> float:
    """Check if common/uncommon tiers should be throttled for this game today.

    Returns multiplier: 1.0 if under limit, 0.5 if over (half pay).
    Only applies to common and uncommon tiers.
    """
    if tier_name not in ("common", "uncommon"):
        return 1.0

    throttle = load_tier_throttle()
    game_key = str(game_id)
    game_data = throttle.get("games", {}).get(game_key, {"common_uncommon_count": 0})
    count = game_data.get("common_uncommon_count", 0)

    if count >= throttle_limit:
        return 0.5
    return 1.0


def increment_tier_throttle(game_id: str, tier_name: str) -> None:
    """Increment the common/uncommon counter for a game today."""
    if tier_name not in ("common", "uncommon"):
        return

    throttle = load_tier_throttle()
    game_key = str(game_id)
    if game_key not in throttle.get("games", {}):
        throttle.setdefault("games", {})[game_key] = {"common_uncommon_count": 0}
    throttle["games"][game_key]["common_uncommon_count"] = (
        throttle["games"][game_key].get("common_uncommon_count", 0) + 1
    )
    save_tier_throttle(throttle)


# ---------------------------------------------------------------------------
# Victory Lap tracking
# ---------------------------------------------------------------------------

def load_victory_lap() -> Dict:
    """Load victory lap state (mastery -> 5x next epoch)."""
    if VICTORY_LAP_PATH.exists():
        try:
            return json.loads(VICTORY_LAP_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"active": False, "game_id": None, "activated_at": None, "epoch_used": False}


def save_victory_lap(data: Dict) -> None:
    """Save victory lap state."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    VICTORY_LAP_PATH.write_text(json.dumps(data, indent=2))


def activate_victory_lap(game_id: str, game_title: str) -> None:
    """Activate victory lap after mastery -- next epoch gets 5x boost."""
    data = {
        "active": True,
        "game_id": game_id,
        "game_title": game_title,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "epoch_used": False,
    }
    save_victory_lap(data)
    log.info("  VICTORY LAP activated for %s! Next epoch gets 5x boost.", game_title)


def consume_victory_lap() -> Optional[Dict]:
    """Check and consume victory lap if active. Returns lap data or None."""
    data = load_victory_lap()
    if data.get("active") and not data.get("epoch_used"):
        data["epoch_used"] = True
        data["consumed_at"] = datetime.now(timezone.utc).isoformat()
        save_victory_lap(data)
        return data
    return None


def get_victory_lap_multiplier() -> float:
    """Return current victory lap multiplier (5.0 if active, 1.0 otherwise)."""
    data = load_victory_lap()
    if data.get("active") and not data.get("epoch_used"):
        return 5.0
    return 1.0


# ---------------------------------------------------------------------------
# Rarity factor calculation
# ---------------------------------------------------------------------------

def get_rarity_factor(unlock_pct: float, rarity_config: Dict) -> float:
    """Calculate rarity multiplier based on RA unlock percentage.

    unlock_pct: percentage of players who have unlocked this achievement (0-100).
    """
    if unlock_pct > 50.0:
        return rarity_config.get("common_above_50pct", 1.0)
    elif unlock_pct > 20.0:
        return rarity_config.get("uncommon_20_50pct", 1.25)
    elif unlock_pct > 5.0:
        return rarity_config.get("rare_5_20pct", 1.75)
    elif unlock_pct > 1.0:
        return rarity_config.get("ultra_rare_1_5pct", 2.5)
    else:
        return rarity_config.get("legendary_below_1pct", 3.0)


# ---------------------------------------------------------------------------
# Proof of Play session boost lookup
# ---------------------------------------------------------------------------

def get_session_boost(config: Dict) -> float:
    """Read current session boost from proof_of_play state file.

    The proof_of_play.py daemon writes session state that we read.
    Returns the boost multiplier (1.0 if no active session).
    """
    session_file = STATE_DIR / "sessions" / "current_session.json"
    if not session_file.exists():
        return 1.0

    try:
        session = json.loads(session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return 1.0

    if not session.get("active", False):
        return 1.0

    return session.get("boost_multiplier", 1.0)


# ---------------------------------------------------------------------------
# Reward tier classification
# ---------------------------------------------------------------------------

def classify_achievement(points: int, tiers: Dict) -> Tuple[Optional[str], float]:
    """Classify an achievement by point value into a reward tier.

    Returns (tier_name, rtc_amount) or (None, 0) if below minimum.
    """
    tier_order = ["legendary", "ultra_rare", "rare", "uncommon", "common"]
    for tier_name in tier_order:
        tier = tiers.get(tier_name)
        if tier is None:
            continue
        if isinstance(tier, dict) and points >= tier.get("min_points", 0):
            return tier_name, tier.get("rtc", 0)

    return None, 0.0


# ---------------------------------------------------------------------------
# RetroAchievements API
# ---------------------------------------------------------------------------

class RetroAchievementsClient:
    """Thin wrapper around the RetroAchievements.org web API."""

    def __init__(self, api_url: str, username: str, api_key: str):
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "rustchain-arcade/2.0"

    def _get(self, endpoint: str, params: Dict = None) -> Any:
        """Make authenticated GET request."""
        params = params or {}
        params["z"] = self.username
        params["y"] = self.api_key
        url = f"{self.api_url}/{endpoint}"

        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("RetroAchievements API error: %s", e)
            return None

    def get_recent_achievements(self, minutes: int = 60) -> Optional[List[Dict]]:
        """Fetch user's recently unlocked achievements.

        GET /API_GetUserRecentAchievements.php?z={}&y={}&u={}&m={}
        """
        data = self._get(
            "API_GetUserRecentAchievements.php",
            {"u": self.username, "m": minutes},
        )
        if isinstance(data, list):
            return data
        return None

    def get_game_progress(self, game_id: int) -> Optional[Dict]:
        """Fetch game info and user progress for mastery check.

        GET /API_GetGameInfoAndUserProgress.php?z={}&y={}&u={}&g={}
        """
        return self._get(
            "API_GetGameInfoAndUserProgress.php",
            {"u": self.username, "g": game_id},
        )

    def get_achievement_unlock_rate(self, game_id: int) -> Dict[str, float]:
        """Fetch unlock percentages for all achievements in a game.

        GET /API_GetGameInfoAndUserProgress.php returns Achievements with
        NumAwarded / NumDistinctPlayers for each achievement.

        Returns dict mapping achievement_id -> unlock_percentage.
        """
        data = self._get(
            "API_GetGameInfoAndUserProgress.php",
            {"u": self.username, "g": game_id},
        )
        if data is None:
            return {}

        total_players = int(data.get("NumDistinctPlayers", 0))
        if total_players == 0:
            return {}

        rates = {}
        achievements = data.get("Achievements", {})
        # Achievements can be a dict keyed by ID or a list
        if isinstance(achievements, dict):
            items = achievements.values()
        elif isinstance(achievements, list):
            items = achievements
        else:
            return {}

        for ach in items:
            ach_id = str(ach.get("ID", ""))
            num_awarded = int(ach.get("NumAwarded", 0))
            pct = (num_awarded / total_players) * 100.0 if total_players > 0 else 50.0
            rates[ach_id] = round(pct, 2)

        return rates

    def get_game_info(self, game_id: int) -> Optional[Dict]:
        """Fetch basic game info.

        GET /API_GetGame.php?z={}&y={}&i={}
        """
        return self._get("API_GetGame.php", {"i": game_id})


# ---------------------------------------------------------------------------
# RTC reward submission
# ---------------------------------------------------------------------------

def submit_achievement_reward(
    config: Dict,
    achievement: Dict,
    tier_name: str,
    rtc_amount: float,
    is_hardcore: bool,
    rarity_factor: float = 1.0,
    session_boost: float = 1.0,
    victory_lap_active: bool = False,
) -> bool:
    """Submit an achievement reward claim to the RustChain node.

    Posts to /api/gaming/achievement on the configured node.
    Falls back to local storage if the endpoint is unavailable.
    """
    node_url = config["rustchain"]["node_url"].rstrip("/")
    verify_ssl = config["rustchain"].get("verify_ssl", False)
    wallet_id = os.environ.get("SOPHIA_WALLET", config.get("node_id", "rustchain-arcade-rpi"))

    payload = {
        "miner": wallet_id,
        "source": "retroachievements",
        "achievement_id": str(achievement.get("AchievementID", achievement.get("ID", ""))),
        "game_id": str(achievement.get("GameID", "")),
        "game_title": achievement.get("GameTitle", ""),
        "achievement_title": achievement.get("Title", ""),
        "points": achievement.get("Points", 0),
        "tier": tier_name,
        "rtc_amount": rtc_amount,
        "hardcore": is_hardcore,
        "rarity_factor": rarity_factor,
        "session_boost": session_boost,
        "victory_lap": victory_lap_active,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    url = f"{node_url}/api/gaming/achievement"
    try:
        resp = requests.post(url, json=payload, verify=verify_ssl, timeout=15)
        if resp.status_code == 200:
            log.info("  Reward submitted to node: %.5f RTC", rtc_amount)
            return True
        else:
            log.warning("  Node rejected reward (HTTP %d): %s", resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("  Could not reach node for reward submission: %s", e)

    # Store locally for later batch submission
    with open(PENDING_REWARDS_PATH, "a") as f:
        f.write(json.dumps(payload) + "\n")
    log.info("  Stored reward locally for batch submission (%.5f RTC)", rtc_amount)
    return False


def submit_mastery_bonus(
    config: Dict,
    game_id: int,
    game_title: str,
    bonus_rtc: float,
    milestone_type: str = "full_mastery",
) -> bool:
    """Submit a mastery milestone bonus claim."""
    node_url = config["rustchain"]["node_url"].rstrip("/")
    verify_ssl = config["rustchain"].get("verify_ssl", False)
    wallet_id = os.environ.get("SOPHIA_WALLET", config.get("node_id", "rustchain-arcade-rpi"))

    payload = {
        "miner": wallet_id,
        "source": "retroachievements",
        "type": "mastery_bonus",
        "milestone": milestone_type,
        "game_id": str(game_id),
        "game_title": game_title,
        "rtc_amount": bonus_rtc,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    url = f"{node_url}/api/gaming/achievement"
    try:
        resp = requests.post(url, json=payload, verify=verify_ssl, timeout=15)
        if resp.status_code == 200:
            log.info("  Mastery milestone (%s) submitted: %.5f RTC for %s",
                     milestone_type, bonus_rtc, game_title)
            return True
    except requests.RequestException:
        pass

    with open(PENDING_REWARDS_PATH, "a") as f:
        f.write(json.dumps(payload) + "\n")
    log.info("  Mastery bonus stored locally: %.5f RTC for %s", bonus_rtc, game_title)
    return False


def submit_pending_rewards(config: Dict) -> int:
    """Try to submit any locally stored pending rewards. Returns count submitted."""
    if not PENDING_REWARDS_PATH.exists():
        return 0

    try:
        lines = PENDING_REWARDS_PATH.read_text().strip().split("\n")
    except OSError:
        return 0

    if not lines or lines == [""]:
        return 0

    node_url = config["rustchain"]["node_url"].rstrip("/")
    verify_ssl = config["rustchain"].get("verify_ssl", False)
    url = f"{node_url}/api/gaming/achievement"

    remaining = []
    submitted = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        try:
            resp = requests.post(url, json=payload, verify=verify_ssl, timeout=15)
            if resp.status_code == 200:
                submitted += 1
            else:
                remaining.append(line)
        except requests.RequestException:
            remaining.append(line)

    # Rewrite pending file with only failed items
    if remaining:
        PENDING_REWARDS_PATH.write_text("\n".join(remaining) + "\n")
    else:
        PENDING_REWARDS_PATH.unlink(missing_ok=True)

    if submitted > 0:
        log.info("Submitted %d pending rewards from local queue", submitted)
    return submitted


# ---------------------------------------------------------------------------
# Mastery milestone detection
# ---------------------------------------------------------------------------

def check_mastery_milestones(
    client: RetroAchievementsClient,
    config: Dict,
    game_id: int,
    game_title: str,
    reported: Dict,
    remaining: float,
) -> float:
    """Check and award mastery milestones for a game.

    Milestones:
      - first_clear: First achievement in a game (checked elsewhere, included for completeness)
      - full_mastery: 100% of achievements
      - legendary_mastery: 100% in hardcore mode
      - system_crown_5_games: 5 masteries on one console/platform

    Returns total RTC spent on milestones.
    """
    milestones_cfg = config.get("mastery_milestones", {})
    mastered_games = set(str(g) for g in reported.get("mastered_games", []))
    milestone_log = reported.get("milestones", {})
    game_key = str(game_id)
    spent = 0.0

    if game_key in mastered_games:
        return 0.0

    progress = client.get_game_progress(game_id)
    if progress is None:
        return 0.0

    num_achievements = int(progress.get("NumAchievements", 0))
    num_awarded = int(progress.get("NumAwardedToUser", 0))
    num_awarded_hc = int(progress.get("NumAwardedToUserHardcore", 0))

    if num_achievements == 0:
        return 0.0

    # Full mastery (100% softcore or hardcore)
    if num_awarded >= num_achievements:
        mastered_games.add(game_key)
        reported["mastered_games"] = list(mastered_games)

        # Determine mastery type
        if num_awarded_hc >= num_achievements:
            # Legendary mastery -- 100% hardcore
            milestone_key = f"{game_key}_legendary"
            if milestone_key not in milestone_log:
                bonus = milestones_cfg.get("legendary_mastery", 0.05)
                if bonus <= remaining:
                    submit_mastery_bonus(config, game_id, game_title, bonus, "legendary_mastery")
                    add_daily_spent(bonus)
                    spent += bonus
                    remaining -= bonus
                    milestone_log[milestone_key] = datetime.now(timezone.utc).isoformat()
                    log.info("  LEGENDARY MASTERY! %s -- all %d achievements in HARDCORE!",
                             game_title, num_achievements)
        else:
            # Standard full mastery
            milestone_key = f"{game_key}_full"
            if milestone_key not in milestone_log:
                bonus = milestones_cfg.get("full_mastery", 0.02)
                if bonus <= remaining:
                    submit_mastery_bonus(config, game_id, game_title, bonus, "full_mastery")
                    add_daily_spent(bonus)
                    spent += bonus
                    remaining -= bonus
                    milestone_log[milestone_key] = datetime.now(timezone.utc).isoformat()
                    log.info("  FULL MASTERY! %s -- all %d achievements unlocked!",
                             game_title, num_achievements)

        # Activate victory lap
        activate_victory_lap(game_key, game_title)

        # Write cartridge relic
        try:
            from cartridge_wallet import CartridgeWallet
            cw = CartridgeWallet()
            platform_name = progress.get("ConsoleName", "Unknown")
            cw.mint_cartridge(
                game_id=int(game_key),
                game_title=game_title,
                platform=platform_name,
                achievement_count=num_achievements,
                hardcore=num_awarded_hc >= num_achievements,
                total_rtc_earned=spent,
            )
        except Exception as e:
            log.debug("Could not write cartridge relic: %s", e)

        # Check system crown (5 masteries on one platform)
        platform_name = progress.get("ConsoleName", "Unknown")
        platform_mastery_count = _count_platform_masteries(reported, platform_name, client)
        crown_key = f"crown_{platform_name}"
        if platform_mastery_count >= 5 and crown_key not in milestone_log:
            crown_bonus = milestones_cfg.get("system_crown_5_games", 0.03)
            if crown_bonus <= remaining:
                submit_mastery_bonus(config, game_id, f"System Crown: {platform_name}",
                                     crown_bonus, "system_crown")
                add_daily_spent(crown_bonus)
                spent += crown_bonus
                milestone_log[crown_key] = datetime.now(timezone.utc).isoformat()
                log.info("  SYSTEM CROWN! 5+ masteries on %s!", platform_name)

    reported["milestones"] = milestone_log
    return spent


def _count_platform_masteries(reported: Dict, platform_name: str, client: RetroAchievementsClient) -> int:
    """Count how many mastered games belong to a specific platform.

    Uses cached cartridge data if available, falls back to API.
    """
    count = 0
    for game_id in reported.get("mastered_games", []):
        cartridge_file = CARTRIDGE_DIR / f"{game_id}.json"
        if cartridge_file.exists():
            try:
                cart = json.loads(cartridge_file.read_text())
                if cart.get("platform", "").lower() == platform_name.lower():
                    count += 1
                continue
            except (json.JSONDecodeError, OSError):
                pass

        # Fallback: query RA API for platform (expensive, cache miss)
        info = client.get_game_info(int(game_id))
        if info and info.get("ConsoleName", "").lower() == platform_name.lower():
            count += 1

    return count


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

def process_achievements(config: Dict) -> None:
    """Single pass: fetch recent achievements, classify, submit rewards."""
    acfg = config.get("achievements", {})
    ra_cfg = acfg.get("retroachievements", {})
    tiers = acfg.get("reward_tiers", {})
    rarity_config = config.get("rarity_factors", {})
    anti_cheat = config.get("anti_cheat", {})
    hardcore_only = anti_cheat.get("hardcore_only", True)
    max_per_hour = anti_cheat.get("max_achievements_per_hour", 20)
    throttle_limit = anti_cheat.get("common_uncommon_daily_throttle", 8)
    hardcore_mult = acfg.get("hardcore_multiplier", 2.0)

    # Daily cap from retro_treasury (per-wallet), falling back to achievements config
    wallet_cap = config.get("retro_treasury", {}).get("wallet_daily_cap", acfg.get("daily_cap_rtc", 0.10))

    username = os.environ.get("RA_USERNAME", ra_cfg.get("username", ""))
    api_key = os.environ.get("RA_API_KEY", ra_cfg.get("api_key", ""))

    if not username or not api_key:
        log.error("RetroAchievements credentials not configured. Set RA_USERNAME and RA_API_KEY.")
        return

    client = RetroAchievementsClient(
        api_url=ra_cfg.get("api_url", "https://retroachievements.org/API"),
        username=username,
        api_key=api_key,
    )

    # Try submitting any pending offline rewards first
    submit_pending_rewards(config)

    # Check daily cap
    daily_spent = get_daily_spent(wallet_cap)
    if daily_spent >= wallet_cap:
        log.info("Daily cap reached (%.5f / %.5f RTC). Skipping.", daily_spent, wallet_cap)
        return

    remaining = wallet_cap - daily_spent

    # Check achievement velocity anti-cheat
    velocity_ok, velocity_count = check_achievement_velocity(max_per_hour)
    if not velocity_ok:
        log.warning("ANTI-CHEAT: Achievement velocity too high (%d/hr, max %d). Pausing rewards.",
                     velocity_count, max_per_hour)
        return

    # Get session boost from proof_of_play daemon
    session_boost = get_session_boost(config)
    if session_boost > 1.0:
        log.info("Proof of Play session boost active: %.1fx", session_boost)

    # Get victory lap multiplier
    victory_lap_mult = get_victory_lap_multiplier()
    victory_lap_active = victory_lap_mult > 1.0
    if victory_lap_active:
        log.info("VICTORY LAP active! %.1fx multiplier this epoch.", victory_lap_mult)

    # Fetch recent achievements (last 60 minutes)
    achievements = client.get_recent_achievements(minutes=60)
    if achievements is None:
        log.warning("Failed to fetch recent achievements")
        return

    if not achievements:
        log.info("No new achievements in the last 60 minutes")
        return

    reported = load_reported()
    reported_ids = set(str(a) for a in reported.get("achievements", []))
    new_reported = False
    games_to_check_mastery = set()

    # Cache unlock rates per game to avoid redundant API calls
    unlock_rate_cache: Dict[str, Dict[str, float]] = {}

    for ach in achievements:
        ach_id = str(ach.get("AchievementID", ach.get("ID", "")))
        if not ach_id or ach_id in reported_ids:
            continue

        points = int(ach.get("Points", 0))
        is_hardcore = bool(ach.get("HardcoreMode", 0))
        game_id = str(ach.get("GameID", ""))
        game_title = ach.get("GameTitle", "Unknown")

        # Anti-cheat: hardcore only mode
        if hardcore_only and not is_hardcore:
            log.info("  [%s] %s - skipping (softcore, hardcore_only mode)", game_title, ach.get("Title", ""))
            reported_ids.add(ach_id)
            new_reported = True
            continue

        tier_name, base_rtc = classify_achievement(points, tiers)
        if tier_name is None:
            log.info("  [%s] %s - %d pts - below minimum, skipping",
                     game_title, ach.get("Title", ""), points)
            reported_ids.add(ach_id)
            new_reported = True
            continue

        # Fetch rarity (unlock rate) for this achievement
        if game_id and game_id not in unlock_rate_cache:
            unlock_rate_cache[game_id] = client.get_achievement_unlock_rate(int(game_id))

        unlock_pct = unlock_rate_cache.get(game_id, {}).get(ach_id, 50.0)
        rarity_factor = get_rarity_factor(unlock_pct, rarity_config)

        # Compute final RTC: base * rarity * hardcore * throttle * session_boost * victory_lap
        rtc_amount = base_rtc * rarity_factor

        if is_hardcore:
            rtc_amount *= hardcore_mult

        # Tier throttle (common/uncommon half-pay after 8/game/day)
        throttle_mult = check_tier_throttle(game_id, tier_name, throttle_limit)
        rtc_amount *= throttle_mult

        # Session boost from proof of play
        rtc_amount *= session_boost

        # Victory lap boost
        if victory_lap_active:
            rtc_amount *= victory_lap_mult
            # Consume the victory lap after first use
            consume_victory_lap()

        # Enforce daily cap
        if rtc_amount > remaining:
            log.info("  Daily cap would be exceeded. Capping at %.5f RTC", remaining)
            rtc_amount = remaining

        if rtc_amount <= 0:
            log.info("  Daily cap reached. Stopping.")
            break

        mode_str = " [HARDCORE]" if is_hardcore else ""
        throttle_str = " [THROTTLED 50%]" if throttle_mult < 1.0 else ""
        rarity_str = f" [rarity:{unlock_pct:.1f}% x{rarity_factor:.2f}]"
        boost_str = f" [boost:x{session_boost:.1f}]" if session_boost > 1.0 else ""
        vlap_str = " [VICTORY LAP]" if victory_lap_active else ""
        log.info("  [%s] %s - %d pts - %s%s%s%s%s%s - %.5f RTC",
                 game_title, ach.get("Title", ""), points, tier_name,
                 mode_str, rarity_str, throttle_str, boost_str, vlap_str, rtc_amount)

        submit_achievement_reward(
            config, ach, tier_name, rtc_amount, is_hardcore,
            rarity_factor=rarity_factor,
            session_boost=session_boost,
            victory_lap_active=victory_lap_active,
        )
        add_daily_spent(rtc_amount)
        remaining -= rtc_amount

        # Record for velocity tracking
        record_achievement_timestamp()

        # Record tier throttle
        increment_tier_throttle(game_id, tier_name)

        reported_ids.add(ach_id)
        new_reported = True

        if game_id:
            games_to_check_mastery.add((int(game_id), game_title))

    # Check for mastery milestones
    for game_id, game_title in games_to_check_mastery:
        milestone_spent = check_mastery_milestones(
            client, config, game_id, game_title, reported, remaining
        )
        remaining -= milestone_spent

    if new_reported:
        reported["achievements"] = list(reported_ids)
        save_reported(reported)


def poll_loop(config: Dict) -> None:
    """Main polling loop."""
    interval = config.get("achievements", {}).get("poll_interval_seconds", 300)
    wallet_cap = config.get("retro_treasury", {}).get(
        "wallet_daily_cap",
        config.get("achievements", {}).get("daily_cap_rtc", 0.10),
    )

    log.info("=== Sophia Achievement Bridge v2.0 ===")
    log.info("Poll interval: %ds", interval)
    log.info("Daily wallet cap: %.4f RTC", wallet_cap)
    log.info("Rarity scoring: enabled")
    log.info("Proof of Play boost: enabled")
    log.info("Anti-cheat: max %d achievements/hr, hardcore_only=%s",
             config.get("anti_cheat", {}).get("max_achievements_per_hour", 20),
             config.get("anti_cheat", {}).get("hardcore_only", True))

    while True:
        try:
            process_achievements(config)
        except Exception:
            log.exception("Error in achievement processing")
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
        log.error("Config not found at %s", cfg_path)
        sys.exit(1)


def main():
    config = load_config()
    if not config.get("achievements", {}).get("enabled", True):
        log.info("Achievements are disabled in config. Exiting.")
        sys.exit(0)
    try:
        poll_loop(config)
    except KeyboardInterrupt:
        log.info("Achievement bridge stopped by user.")


if __name__ == "__main__":
    main()
