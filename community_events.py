#!/usr/bin/env python3
"""
rustchain-arcade: Community Events System

Weekly and seasonal event system for retro gaming rewards.

Features:
  - Saturday Morning Quests: weekly featured platform with 1.05x bonus
  - Cabinet Hunts: community goal tracking (e.g., "clear 50 Genesis bosses")
  - Arcade Seasons: quarterly ranking by unique masteries and platform variety
  - One-Credit Club: track clean single-session clears for prestige badges
  - Fetches active events from RustChain node API (or local fallback)
  - Stores event participation in ~/.rustchain-arcade/events/
"""

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("sophia-community-events")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
EVENTS_DIR = STATE_DIR / "events"
ACTIVE_EVENTS_PATH = EVENTS_DIR / "active_events.json"
PARTICIPATION_PATH = EVENTS_DIR / "participation.json"
ONE_CREDIT_PATH = EVENTS_DIR / "one_credit_club.json"
SEASON_PATH = EVENTS_DIR / "current_season.json"

# ---------------------------------------------------------------------------
# Rotating platform schedule for Saturday Morning Quests
# ---------------------------------------------------------------------------

# Each week features a different platform. Rotates through this list based
# on the ISO week number. Players get a 1.05x bonus on the featured platform.
SATURDAY_PLATFORMS = [
    "NES",
    "SNES",
    "Genesis/Mega Drive",
    "Game Boy",
    "Game Boy Advance",
    "Nintendo 64",
    "PlayStation",
    "Atari 2600",
    "Master System",
    "TurboGrafx-16",
    "Neo Geo",
    "Game Boy Color",
    "Arcade",
    "Atari 7800",
    "Game Gear",
    "Saturn",
    "Nintendo DS",
    "Virtual Boy",
    "Dreamcast",
    "Lynx",
    "WonderSwan",
    "ColecoVision",
    "32X",
    "SG-1000",
]


def get_current_week_platform() -> str:
    """Get the featured platform for this week's Saturday Morning Quest.

    Rotates through the platform list based on ISO week number.
    """
    now = datetime.now(timezone.utc)
    iso_week = now.isocalendar()[1]
    idx = iso_week % len(SATURDAY_PLATFORMS)
    return SATURDAY_PLATFORMS[idx]


def is_saturday() -> bool:
    """Check if today is Saturday (UTC)."""
    return datetime.now(timezone.utc).weekday() == 5


def get_current_quarter() -> Dict:
    """Get current arcade season quarter info."""
    now = datetime.now(timezone.utc)
    quarter = (now.month - 1) // 3 + 1
    year = now.year
    quarter_start_month = (quarter - 1) * 3 + 1
    start = datetime(year, quarter_start_month, 1, tzinfo=timezone.utc)

    if quarter == 4:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, quarter_start_month + 3, 1, tzinfo=timezone.utc)

    days_in = (now - start).days
    days_total = (end - start).days

    return {
        "quarter": quarter,
        "year": year,
        "season_name": f"Season {year}Q{quarter}",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days_elapsed": days_in,
        "days_total": days_total,
        "progress_pct": round((days_in / days_total) * 100, 1) if days_total > 0 else 0,
    }


# ---------------------------------------------------------------------------
# Event fetching
# ---------------------------------------------------------------------------

def fetch_active_events(config: Dict) -> List[Dict]:
    """Fetch active community events from RustChain node.

    Falls back to locally generated events if node is unreachable.
    """
    node_url = config.get("rustchain", {}).get("node_url", "").rstrip("/")
    verify_ssl = config.get("rustchain", {}).get("verify_ssl", False)

    if node_url:
        url = f"{node_url}/api/gaming/events"
        try:
            resp = requests.get(url, verify=verify_ssl, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    return data
                elif isinstance(data, dict) and "events" in data:
                    return data["events"]
        except (requests.RequestException, json.JSONDecodeError):
            log.debug("Could not fetch events from node, using local fallback")

    # Local fallback: generate events based on date
    return generate_local_events(config)


def generate_local_events(config: Dict) -> List[Dict]:
    """Generate community events locally when the node is unreachable.

    Always available: Saturday Morning Quests and Arcade Seasons.
    """
    events = []
    events_cfg = config.get("community_events", {})
    now = datetime.now(timezone.utc)

    # Saturday Morning Quest
    smq_cfg = events_cfg.get("saturday_morning_quests", {})
    if smq_cfg.get("enabled", True):
        featured_platform = get_current_week_platform()
        iso_year, iso_week, _ = now.isocalendar()

        # Quest runs all week, but Saturday is the "big day"
        events.append({
            "event_type": "saturday_morning_quest",
            "name": f"Saturday Morning Quest: {featured_platform}",
            "description": f"This week's featured platform is {featured_platform}! "
                           f"Earn a 1.05x bonus on all {featured_platform} achievements.",
            "featured_platform": featured_platform,
            "bonus_multiplier": smq_cfg.get("bonus_multiplier", 1.05),
            "week": f"{iso_year}-W{iso_week:02d}",
            "is_saturday": is_saturday(),
            "active": True,
        })

    # Cabinet Hunt (community boss kill goals)
    hunt_cfg = events_cfg.get("cabinet_hunts", {})
    if hunt_cfg.get("enabled", True):
        # Generate a rotating cabinet hunt theme based on week
        iso_week = now.isocalendar()[1]
        hunt_themes = [
            {"goal": "Clear 50 boss fights across Genesis games", "platform": "Genesis/Mega Drive", "target": 50},
            {"goal": "Beat 25 NES games to completion", "platform": "NES", "target": 25},
            {"goal": "Earn 100 SNES achievements this weekend", "platform": "SNES", "target": 100},
            {"goal": "Master 10 Game Boy games network-wide", "platform": "Game Boy", "target": 10},
            {"goal": "Unlock 75 Arcade achievements", "platform": "Arcade", "target": 75},
            {"goal": "Complete 30 PlayStation challenges", "platform": "PlayStation", "target": 30},
        ]
        hunt = hunt_themes[iso_week % len(hunt_themes)]
        events.append({
            "event_type": "cabinet_hunt",
            "name": f"Cabinet Hunt: {hunt['platform']}",
            "description": hunt["goal"],
            "target_platform": hunt["platform"],
            "community_target": hunt["target"],
            "active": True,
        })

    # Arcade Season
    season_cfg = events_cfg.get("arcade_seasons", {})
    if season_cfg.get("enabled", True):
        quarter_info = get_current_quarter()
        events.append({
            "event_type": "arcade_season",
            "name": quarter_info["season_name"],
            "description": f"Season rankings: unique masteries and platform variety. "
                           f"{quarter_info['days_total'] - quarter_info['days_elapsed']} days remaining.",
            "quarter": quarter_info,
            "active": True,
        })

    # One-Credit Club (persistent, always active)
    occ_cfg = events_cfg.get("one_credit_club", {})
    if occ_cfg.get("enabled", True):
        events.append({
            "event_type": "one_credit_club",
            "name": "One-Credit Club",
            "description": "Master a game in a single unbroken session for prestige. "
                           "No saves, no continues, one sitting.",
            "active": True,
        })

    return events


# ---------------------------------------------------------------------------
# Participation tracking
# ---------------------------------------------------------------------------

def load_participation() -> Dict:
    """Load event participation records."""
    if PARTICIPATION_PATH.exists():
        try:
            return json.loads(PARTICIPATION_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"events": {}, "weekly_platforms": {}}


def save_participation(data: Dict) -> None:
    """Save event participation records."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    PARTICIPATION_PATH.write_text(json.dumps(data, indent=2))


def record_event_participation(event_type: str, event_name: str, details: Dict) -> None:
    """Record participation in a community event."""
    participation = load_participation()
    events = participation.setdefault("events", {})
    event_key = f"{event_type}:{datetime.now(timezone.utc).strftime('%Y-W%W')}"

    if event_key not in events:
        events[event_key] = {
            "event_type": event_type,
            "event_name": event_name,
            "first_participation": datetime.now(timezone.utc).isoformat(),
            "actions": [],
        }

    events[event_key]["actions"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **details,
    })
    events[event_key]["last_participation"] = datetime.now(timezone.utc).isoformat()

    save_participation(participation)


def check_saturday_morning_bonus(platform: str, config: Dict) -> float:
    """Check if an achievement qualifies for Saturday Morning Quest bonus.

    Returns the bonus multiplier (1.05x if platform matches this week's featured,
    1.0 otherwise).
    """
    events_cfg = config.get("community_events", {})
    smq_cfg = events_cfg.get("saturday_morning_quests", {})
    if not smq_cfg.get("enabled", True):
        return 1.0

    featured = get_current_week_platform()
    platform_lower = platform.lower()
    featured_lower = featured.lower()

    # Check if the platform matches (fuzzy: "Genesis/Mega Drive" matches both)
    featured_parts = [p.strip().lower() for p in featured_lower.split("/")]

    if platform_lower in featured_parts or featured_lower in platform_lower:
        bonus = smq_cfg.get("bonus_multiplier", 1.05)
        record_event_participation(
            "saturday_morning_quest",
            f"Saturday Morning Quest: {featured}",
            {"platform": platform, "bonus": bonus},
        )
        return bonus

    return 1.0


# ---------------------------------------------------------------------------
# One-Credit Club
# ---------------------------------------------------------------------------

def load_one_credit_club() -> Dict:
    """Load One-Credit Club records."""
    if ONE_CREDIT_PATH.exists():
        try:
            return json.loads(ONE_CREDIT_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"clears": [], "total_one_credits": 0}


def save_one_credit_club(data: Dict) -> None:
    """Save One-Credit Club records."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    ONE_CREDIT_PATH.write_text(json.dumps(data, indent=2))


def record_one_credit_clear(game_id: int, game_title: str, platform: str,
                             session_duration_minutes: float) -> None:
    """Record a one-credit clear: game mastered in a single unbroken session.

    A one-credit clear means mastery was achieved without the session ending
    (no saves, no quits, one continuous play session).
    """
    club = load_one_credit_club()

    clear = {
        "game_id": game_id,
        "game_title": game_title,
        "platform": platform,
        "session_minutes": round(session_duration_minutes, 1),
        "cleared_at": datetime.now(timezone.utc).isoformat(),
    }
    club["clears"].append(clear)
    club["total_one_credits"] = len(club["clears"])
    save_one_credit_club(club)

    log.info("ONE-CREDIT CLEAR! %s (%s) in %.1f minutes!",
             game_title, platform, session_duration_minutes)

    record_event_participation(
        "one_credit_club",
        "One-Credit Club",
        {"game_id": game_id, "game_title": game_title, "minutes": session_duration_minutes},
    )


def check_one_credit_eligibility(game_id: int, session_start_ts: float) -> bool:
    """Check if current session qualifies as a one-credit clear.

    Criteria: mastery achieved, session still active (never ended since start).
    We check by verifying the session file's started_at_ts matches.
    """
    session_file = STATE_DIR / "sessions" / "current_session.json"
    if not session_file.exists():
        return False

    try:
        session = json.loads(session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    if not session.get("active", False):
        return False

    # Session must have started at or before the given timestamp
    # (within 5 seconds tolerance for clock skew)
    sess_start = session.get("started_at_ts", 0)
    if abs(sess_start - session_start_ts) > 5:
        return False

    return True


# ---------------------------------------------------------------------------
# Arcade Season tracking
# ---------------------------------------------------------------------------

def load_season_stats() -> Dict:
    """Load current arcade season statistics."""
    if SEASON_PATH.exists():
        try:
            data = json.loads(SEASON_PATH.read_text())
            # Check if season is current
            quarter = get_current_quarter()
            if data.get("season_name") == quarter["season_name"]:
                return data
        except (json.JSONDecodeError, OSError):
            pass

    # New season
    quarter = get_current_quarter()
    return {
        "season_name": quarter["season_name"],
        "quarter": quarter["quarter"],
        "year": quarter["year"],
        "unique_masteries": [],
        "platforms_played": [],
        "total_achievements": 0,
        "total_rtc": 0.0,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def save_season_stats(data: Dict) -> None:
    """Save current arcade season statistics."""
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    SEASON_PATH.write_text(json.dumps(data, indent=2))


def record_season_achievement(game_id: int, game_title: str, platform: str,
                               rtc_earned: float, is_mastery: bool = False) -> None:
    """Record an achievement/mastery for the current arcade season."""
    stats = load_season_stats()

    stats["total_achievements"] = stats.get("total_achievements", 0) + 1
    stats["total_rtc"] = round(stats.get("total_rtc", 0.0) + rtc_earned, 6)

    # Track unique platforms
    platforms = stats.get("platforms_played", [])
    if platform not in platforms:
        platforms.append(platform)
        stats["platforms_played"] = platforms

    # Track unique masteries
    if is_mastery:
        masteries = stats.get("unique_masteries", [])
        game_key = str(game_id)
        if game_key not in masteries:
            masteries.append(game_key)
            stats["unique_masteries"] = masteries

    save_season_stats(stats)


def get_season_summary() -> Dict:
    """Get a summary of the current arcade season performance."""
    stats = load_season_stats()
    quarter = get_current_quarter()

    return {
        "season": stats.get("season_name", quarter["season_name"]),
        "unique_masteries": len(stats.get("unique_masteries", [])),
        "platforms_played": len(stats.get("platforms_played", [])),
        "platform_variety_score": len(stats.get("platforms_played", [])) * 10,
        "total_achievements": stats.get("total_achievements", 0),
        "total_rtc": stats.get("total_rtc", 0.0),
        "days_remaining": quarter["days_total"] - quarter["days_elapsed"],
        "progress_pct": quarter["progress_pct"],
    }


# ---------------------------------------------------------------------------
# Event display
# ---------------------------------------------------------------------------

def display_active_events(config: Dict) -> None:
    """Fetch and display all active community events."""
    events = fetch_active_events(config)

    if not events:
        print("\n  No active community events right now.\n")
        return

    print("\n  === Active Community Events ===\n")

    for event in events:
        etype = event.get("event_type", "unknown")
        name = event.get("name", "Unnamed Event")
        desc = event.get("description", "")

        if etype == "saturday_morning_quest":
            star = " ** TODAY! **" if event.get("is_saturday") else ""
            print(f"  [{etype.upper()}] {name}{star}")
            print(f"    {desc}")
            print(f"    Bonus: {event.get('bonus_multiplier', 1.05)}x on featured platform")
            print()

        elif etype == "cabinet_hunt":
            print(f"  [{etype.upper()}] {name}")
            print(f"    {desc}")
            print(f"    Community target: {event.get('community_target', '?')}")
            print()

        elif etype == "arcade_season":
            quarter = event.get("quarter", {})
            summary = get_season_summary()
            print(f"  [{etype.upper()}] {name}")
            print(f"    {desc}")
            print(f"    Your stats: {summary['unique_masteries']} masteries, "
                  f"{summary['platforms_played']} platforms, "
                  f"{summary['total_achievements']} achievements")
            print(f"    Variety score: {summary['platform_variety_score']}")
            print()

        elif etype == "one_credit_club":
            club = load_one_credit_club()
            print(f"  [{etype.upper()}] {name}")
            print(f"    {desc}")
            print(f"    Your one-credit clears: {club.get('total_one_credits', 0)}")
            print()

        else:
            print(f"  [{etype.upper()}] {name}")
            print(f"    {desc}")
            print()


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
    """CLI for viewing community events and participation."""
    import argparse
    parser = argparse.ArgumentParser(description="Sophia Edge Node -- Community Events")
    parser.add_argument("--events", action="store_true", help="Show active events")
    parser.add_argument("--season", action="store_true", help="Show current season stats")
    parser.add_argument("--one-credit", action="store_true", help="Show One-Credit Club records")
    parser.add_argument("--featured", action="store_true", help="Show this week's featured platform")
    args = parser.parse_args()

    config = load_config()
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.season:
        summary = get_season_summary()
        print(json.dumps(summary, indent=2))
    elif args.one_credit:
        club = load_one_credit_club()
        if club.get("clears"):
            print(f"\n  === One-Credit Club ({club['total_one_credits']} clears) ===\n")
            for clear in club["clears"]:
                print(f"  {clear['game_title']} ({clear['platform']}) -- "
                      f"{clear['session_minutes']} min -- {clear['cleared_at'][:10]}")
        else:
            print("\n  No one-credit clears yet. Master a game in a single session!\n")
    elif args.featured:
        platform = get_current_week_platform()
        print(f"\n  This week's Saturday Morning Quest platform: {platform}\n")
        if is_saturday():
            print("  ** It's Saturday! Bonus is active! **\n")
    else:
        display_active_events(config)


if __name__ == "__main__":
    main()
