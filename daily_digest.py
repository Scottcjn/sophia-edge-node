#!/usr/bin/env python3
"""
rustchain-arcade: Daily Gaming Summary Digest

Generates a daily summary of gaming activity from ~/.rustchain-arcade/ state files.
Compiles games played, achievements unlocked, RTC earned, session duration,
and new cartridge relics.

Can output to terminal, save as ASCII card, or post to Discord webhook.
Designed to run via cron or systemd timer at midnight UTC.

CLI usage:
    python3 daily_digest.py
    python3 daily_digest.py --post-discord
    python3 daily_digest.py --save-card
    python3 daily_digest.py --date 2026-03-20
    python3 daily_digest.py --json
"""

import argparse
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
log = logging.getLogger("sophia-digest")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
SESSIONS_DIR = STATE_DIR / "sessions"
CARTRIDGE_DIR = STATE_DIR / "cartridges"
EVENTS_DIR = STATE_DIR / "events"
DIGEST_DIR = STATE_DIR / "digests"

# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def get_date_range(date_str: str) -> tuple:
    """Get UTC timestamp range for a given date string (YYYY-MM-DD).

    Returns (start_ts, end_ts) as Unix timestamps.
    """
    date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def collect_daily_achievements(date_str: str) -> Dict:
    """Collect achievement data for a specific date.

    Reads from daily_rewards.json and velocity_tracker.json.
    """
    start_ts, end_ts = get_date_range(date_str)

    total_rtc = 0.0
    total_achievements = 0
    hardcore_count = 0
    claims = []

    # Read daily rewards
    daily_path = STATE_DIR / "daily_rewards.json"
    if daily_path.exists():
        try:
            data = json.loads(daily_path.read_text())
            if data.get("date") == date_str:
                total_rtc = data.get("total_rtc", 0.0)
                for claim in data.get("claims", []):
                    total_achievements += 1
                    claims.append(claim)
        except (json.JSONDecodeError, OSError):
            pass

    # Read velocity tracker for achievement timestamps
    velocity_path = STATE_DIR / "velocity_tracker.json"
    if velocity_path.exists():
        try:
            vdata = json.loads(velocity_path.read_text())
            timestamps = vdata.get("timestamps", [])
            for ts in timestamps:
                if start_ts <= ts < end_ts:
                    hardcore_count += 1  # velocity tracker records during active play
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "total_rtc": round(total_rtc, 6),
        "total_achievements": total_achievements,
        "hardcore_count": min(hardcore_count, total_achievements),
        "claims": claims,
    }


def collect_daily_sessions(date_str: str) -> Dict:
    """Collect session data for a specific date from history.jsonl."""
    start_ts, end_ts = get_date_range(date_str)

    total_minutes = 0.0
    total_heartbeats = 0
    games_played = set()
    sessions = []
    best_boost = 1.0

    history_path = SESSIONS_DIR / "history.jsonl"
    if history_path.exists():
        try:
            for line in history_path.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    session = json.loads(line)
                    started = session.get("started_at", "")
                    if started:
                        try:
                            ts = datetime.fromisoformat(started).timestamp()
                            if not (start_ts <= ts < end_ts):
                                continue
                        except (ValueError, TypeError):
                            continue

                    duration = session.get("duration_minutes", 0)
                    total_minutes += duration
                    total_heartbeats += session.get("heartbeat_count", 0)

                    game_id = session.get("game_id")
                    if game_id:
                        games_played.add(game_id)

                    boost = session.get("boost_multiplier", 1.0)
                    if boost > best_boost:
                        best_boost = boost

                    sessions.append({
                        "game_id": game_id,
                        "duration_minutes": duration,
                        "heartbeats": session.get("heartbeat_count", 0),
                        "boost": boost,
                        "achievements": session.get("achievements_earned", 0),
                    })
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    # Also check current session if it started today
    current_path = SESSIONS_DIR / "current_session.json"
    if current_path.exists():
        try:
            current = json.loads(current_path.read_text())
            if current.get("active"):
                started = current.get("started_at", "")
                if started:
                    try:
                        ts = datetime.fromisoformat(started).timestamp()
                        if start_ts <= ts < end_ts:
                            total_minutes += current.get("duration_minutes", 0)
                            total_heartbeats += current.get("heartbeat_count", 0)
                            game_id = current.get("game_id")
                            if game_id:
                                games_played.add(game_id)
                            boost = current.get("boost_multiplier", 1.0)
                            if boost > best_boost:
                                best_boost = boost
                    except (ValueError, TypeError):
                        pass
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "total_minutes": round(total_minutes, 1),
        "total_heartbeats": total_heartbeats,
        "games_played": len(games_played),
        "best_boost": best_boost,
        "sessions": sessions,
    }


def collect_daily_cartridges(date_str: str) -> List[Dict]:
    """Find cartridge relics minted on a specific date."""
    start_ts, end_ts = get_date_range(date_str)
    new_cartridges = []

    if not CARTRIDGE_DIR.exists():
        return new_cartridges

    for path in CARTRIDGE_DIR.glob("*.json"):
        try:
            cart = json.loads(path.read_text())
            minted_at = cart.get("minted_at", "")
            if minted_at:
                try:
                    ts = datetime.fromisoformat(minted_at).timestamp()
                    if start_ts <= ts < end_ts:
                        new_cartridges.append({
                            "game_title": cart.get("game_title", "Unknown"),
                            "platform": cart.get("platform", "Unknown"),
                            "hardcore": cart.get("hardcore_mode", False),
                            "achievements": cart.get("achievement_count", 0),
                            "first_press": cart.get("first_press", False),
                        })
                except (ValueError, TypeError):
                    pass
        except (json.JSONDecodeError, OSError):
            continue

    return new_cartridges


def collect_daily_events(date_str: str) -> Dict:
    """Collect event participation for a specific date."""
    start_ts, end_ts = get_date_range(date_str)

    saturday_bonus_used = False
    one_credit_clears = 0

    # Check participation
    participation_path = EVENTS_DIR / "participation.json"
    if participation_path.exists():
        try:
            data = json.loads(participation_path.read_text())
            for key, event in data.get("events", {}).items():
                if "saturday_morning_quest" in key:
                    for action in event.get("actions", []):
                        ts_str = action.get("timestamp", "")
                        if ts_str:
                            try:
                                ts = datetime.fromisoformat(ts_str).timestamp()
                                if start_ts <= ts < end_ts:
                                    saturday_bonus_used = True
                            except (ValueError, TypeError):
                                pass
        except (json.JSONDecodeError, OSError):
            pass

    # Check one-credit clears
    occ_path = EVENTS_DIR / "one_credit_club.json"
    if occ_path.exists():
        try:
            occ = json.loads(occ_path.read_text())
            for clear in occ.get("clears", []):
                cleared_at = clear.get("cleared_at", "")
                if cleared_at:
                    try:
                        ts = datetime.fromisoformat(cleared_at).timestamp()
                        if start_ts <= ts < end_ts:
                            one_credit_clears += 1
                    except (ValueError, TypeError):
                        pass
        except (json.JSONDecodeError, OSError):
            pass

    return {
        "saturday_bonus_used": saturday_bonus_used,
        "one_credit_clears": one_credit_clears,
    }


def compile_daily_digest(date_str: str) -> Dict:
    """Compile all daily stats into a single digest dict."""
    achievements = collect_daily_achievements(date_str)
    sessions = collect_daily_sessions(date_str)
    cartridges = collect_daily_cartridges(date_str)
    events = collect_daily_events(date_str)

    # Format session time
    total_min = sessions["total_minutes"]
    hours = int(total_min // 60)
    mins = int(total_min % 60)
    session_str = f"{hours}h {mins:02d}m" if hours > 0 else f"{mins}m"

    digest = {
        "date": date_str,
        "wallet": os.environ.get("SOPHIA_WALLET", "unknown"),
        "games_played": sessions["games_played"],
        "total_achievements": achievements["total_achievements"],
        "hardcore_achievements": achievements["hardcore_count"],
        "rtc_earned": achievements["total_rtc"],
        "session_time": session_str,
        "session_minutes": total_min,
        "best_boost": sessions["best_boost"],
        "total_heartbeats": sessions["total_heartbeats"],
        "new_cartridge_relics": len(cartridges),
        "cartridges": cartridges,
        "saturday_bonus": events["saturday_bonus_used"],
        "one_credit_clears": events["one_credit_clears"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    return digest


# ---------------------------------------------------------------------------
# ASCII card formatting
# ---------------------------------------------------------------------------

def format_ascii_card(digest: Dict) -> str:
    """Format digest as an ASCII art card for terminal or sharing."""
    date = digest["date"]
    games = digest["games_played"]
    achievements = digest["total_achievements"]
    hardcore = digest["hardcore_achievements"]
    rtc = digest["rtc_earned"]
    session = digest["session_time"]
    cartridges = digest["new_cartridge_relics"]
    boost = digest["best_boost"]
    one_credit = digest["one_credit_clears"]

    # Achievement detail string
    ach_str = f"{achievements}"
    if hardcore > 0:
        ach_str += f" ({hardcore} hardcore)"

    # Cartridge detail
    cart_str = f"{cartridges}"
    if digest["cartridges"]:
        titles = [c["game_title"] for c in digest["cartridges"][:3]]
        cart_detail = ", ".join(titles)
        if len(cart_detail) > 32:
            cart_detail = cart_detail[:29] + "..."
        cart_str += f" ({cart_detail})"

    # Boost string
    boost_str = f"{boost:.1f}x" if boost > 1.0 else "1.0x (no session)"
    if digest["session_minutes"] >= 60:
        boost_note = f"({int(digest['session_minutes'])}min session)"
    elif digest["session_minutes"] >= 30:
        boost_note = f"({int(digest['session_minutes'])}min session)"
    elif digest["session_minutes"] >= 15:
        boost_note = f"({int(digest['session_minutes'])}min session)"
    else:
        boost_note = ""

    # Extras line
    extras = []
    if digest.get("saturday_bonus"):
        extras.append("Saturday Bonus Active")
    if one_credit > 0:
        extras.append(f"{one_credit} One-Credit Clear(s)!")

    # Build the card
    width = 48
    border = "=" * width
    thin_border = "-" * width

    lines = [
        f"+{border}+",
        f"|{'DAILY GAMING DIGEST':^{width}s}|",
        f"|{date:^{width}s}|",
        f"+{border}+",
        f"|  Games Played: {games:<{width - 17}d}|",
        f"|  Achievements: {ach_str:<{width - 17}s}|",
        f"|  RTC Earned:   {rtc:<{width - 17}.5f}|",
        f"|  Session Time: {session:<{width - 17}s}|",
        f"|  Proof of Play Boost: {boost_str:<{width - 24}s}|",
    ]

    if boost_note:
        lines.append(f"|    {boost_note:<{width - 4}s}|")

    lines.append(f"|  New Cartridge Relics: {cart_str:<{width - 25}s}|")

    if extras:
        lines.append(f"+{thin_border}+")
        for extra in extras:
            lines.append(f"|  * {extra:<{width - 4}s}|")

    lines.append(f"+{border}+")

    # Add signature
    lines.append(f"|{'rustchain-arcade | rustchain.org':^{width}s}|")
    lines.append(f"+{border}+")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Discord webhook posting
# ---------------------------------------------------------------------------

def post_to_discord(digest: Dict, webhook_url: str) -> bool:
    """Post digest to a Discord webhook.

    Formats as an embed with color based on RTC earned.
    """
    if not webhook_url:
        log.warning("No Discord webhook URL configured")
        return False

    # Color based on RTC earned
    rtc = digest["rtc_earned"]
    if rtc >= 0.05:
        color = 0xFFD700  # Gold
    elif rtc >= 0.01:
        color = 0x7B68EE  # Purple
    elif rtc > 0:
        color = 0x32CD32  # Green
    else:
        color = 0x808080  # Gray

    # Build embed fields
    fields = [
        {"name": "Games Played", "value": str(digest["games_played"]), "inline": True},
        {"name": "Achievements", "value": str(digest["total_achievements"]), "inline": True},
        {"name": "RTC Earned", "value": f"{rtc:.5f}", "inline": True},
        {"name": "Session Time", "value": digest["session_time"], "inline": True},
        {"name": "Proof of Play Boost", "value": f"{digest['best_boost']:.1f}x", "inline": True},
        {"name": "New Cartridges", "value": str(digest["new_cartridge_relics"]), "inline": True},
    ]

    if digest.get("hardcore_achievements", 0) > 0:
        fields.append({
            "name": "Hardcore",
            "value": f"{digest['hardcore_achievements']} achievements",
            "inline": True,
        })

    if digest.get("one_credit_clears", 0) > 0:
        fields.append({
            "name": "One-Credit Clears",
            "value": str(digest["one_credit_clears"]),
            "inline": True,
        })

    # Cartridge titles
    if digest.get("cartridges"):
        titles = [f"* {c['game_title']} ({c['platform']})" for c in digest["cartridges"][:5]]
        fields.append({
            "name": "New Cartridge Relics",
            "value": "\n".join(titles),
            "inline": False,
        })

    embed = {
        "title": f"Daily Gaming Digest -- {digest['date']}",
        "color": color,
        "fields": fields,
        "footer": {"text": "rustchain-arcade | rustchain.org"},
        "timestamp": digest.get("generated_at", datetime.now(timezone.utc).isoformat()),
    }

    payload = {
        "embeds": [embed],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            log.info("Digest posted to Discord")
            return True
        else:
            log.warning("Discord webhook returned HTTP %d: %s",
                        resp.status_code, resp.text[:200])
    except requests.RequestException as e:
        log.warning("Failed to post to Discord: %s", e)

    return False


# ---------------------------------------------------------------------------
# Digest storage
# ---------------------------------------------------------------------------

def save_digest(digest: Dict, date_str: str) -> Path:
    """Save digest as JSON and ASCII card to ~/.rustchain-arcade/digests/."""
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)

    # Save JSON
    json_path = DIGEST_DIR / f"{date_str}.json"
    json_path.write_text(json.dumps(digest, indent=2))

    # Save ASCII card
    card = format_ascii_card(digest)
    card_path = DIGEST_DIR / f"{date_str}.txt"
    card_path.write_text(card + "\n")

    return card_path


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
        description="Sophia Edge Node -- Daily Gaming Digest"
    )
    parser.add_argument(
        "--date", type=str, default=None,
        help="Date to generate digest for (YYYY-MM-DD, default: yesterday)"
    )
    parser.add_argument(
        "--today", action="store_true",
        help="Generate digest for today (partial day)"
    )
    parser.add_argument(
        "--post-discord", action="store_true",
        help="Post digest to configured Discord webhook"
    )
    parser.add_argument(
        "--save-card", action="store_true",
        help="Save ASCII card to ~/.rustchain-arcade/digests/"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress terminal output (for cron)"
    )
    args = parser.parse_args()

    config = load_config()

    # Determine date
    if args.date:
        date_str = args.date
    elif args.today:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        # Default to yesterday (for midnight cron run)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")

    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        print(f"Invalid date format: {date_str} (expected YYYY-MM-DD)")
        sys.exit(1)

    # Compile digest
    digest = compile_daily_digest(date_str)

    # Always save
    card_path = save_digest(digest, date_str)

    # Output
    if args.json:
        print(json.dumps(digest, indent=2))
    elif not args.quiet:
        card = format_ascii_card(digest)
        print(card)

    if args.save_card:
        if not args.quiet:
            print(f"\n  Card saved to: {card_path}")

    if args.post_discord:
        discord_cfg = config.get("discord", {})
        webhook_url = discord_cfg.get("webhook_url", "")
        if webhook_url and discord_cfg.get("enabled", False):
            post_to_discord(digest, webhook_url)
        else:
            log.warning("Discord not configured or disabled in config.json")

    # Log summary
    log.info("Digest for %s: %d games, %d achievements, %.5f RTC, %s",
             date_str, digest["games_played"], digest["total_achievements"],
             digest["rtc_earned"], digest["session_time"])


if __name__ == "__main__":
    main()
