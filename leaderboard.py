#!/usr/bin/env python3
"""
rustchain-arcade: Local + Network Leaderboard

Track and display rankings for RTC earnings, masteries, platform variety,
and hardcore completion rate.

Local mode reads from ~/.rustchain-arcade/ state files.
Network mode queries the RustChain API for global rankings.

CLI usage:
    python3 leaderboard.py --local
    python3 leaderboard.py --network
    python3 leaderboard.py --local --period weekly
    python3 leaderboard.py --network --sort masteries
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
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
log = logging.getLogger("sophia-leaderboard")

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
LEADERBOARD_CACHE = STATE_DIR / "leaderboard_cache.json"

# ---------------------------------------------------------------------------
# Time period helpers
# ---------------------------------------------------------------------------

def get_period_start(period: str) -> float:
    """Return Unix timestamp for the start of the given period."""
    now = datetime.now(timezone.utc)

    if period == "weekly":
        # Start of current ISO week (Monday 00:00 UTC)
        days_since_monday = now.weekday()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start -= timedelta(days=days_since_monday)
        return start.timestamp()

    elif period == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp()

    elif period == "season":
        # Start of current quarter
        quarter = (now.month - 1) // 3
        quarter_start_month = quarter * 3 + 1
        start = datetime(now.year, quarter_start_month, 1, tzinfo=timezone.utc)
        return start.timestamp()

    else:  # all-time
        return 0.0


def period_label(period: str) -> str:
    """Human-readable label for a time period."""
    labels = {
        "weekly": "This Week",
        "monthly": "This Month",
        "season": "This Season",
        "all": "All Time",
    }
    return labels.get(period, period.title())


# ---------------------------------------------------------------------------
# Local leaderboard data collection
# ---------------------------------------------------------------------------

def collect_local_stats(period: str = "all") -> Dict:
    """Collect local player stats from ~/.rustchain-arcade/ state files.

    Returns a dict with all stats needed for leaderboard display.
    """
    period_start = get_period_start(period)
    stats = {
        "wallet": os.environ.get("SOPHIA_WALLET", "unknown"),
        "period": period,
        "period_label": period_label(period),
        "total_rtc": 0.0,
        "unique_masteries": 0,
        "total_achievements": 0,
        "platforms_played": [],
        "platform_variety_score": 0,
        "hardcore_masteries": 0,
        "hardcore_rate": 0.0,
        "total_session_minutes": 0.0,
        "games_played": 0,
        "cartridge_relics": 0,
        "system_crowns": 0,
        "first_press_count": 0,
        "one_credit_clears": 0,
        "season_name": "",
    }

    # Read daily rewards for RTC total
    daily_path = STATE_DIR / "daily_rewards.json"
    if daily_path.exists():
        try:
            daily = json.loads(daily_path.read_text())
            claims = daily.get("claims", [])
            for claim in claims:
                claim_time = claim.get("time", "")
                if claim_time:
                    try:
                        ts = datetime.fromisoformat(claim_time).timestamp()
                        if ts >= period_start:
                            stats["total_rtc"] += claim.get("amount", 0.0)
                    except (ValueError, TypeError):
                        stats["total_rtc"] += claim.get("amount", 0.0)
                else:
                    stats["total_rtc"] += claim.get("amount", 0.0)
        except (json.JSONDecodeError, OSError):
            pass

    # Read cartridge relics for mastery stats
    if CARTRIDGE_DIR.exists():
        platforms = set()
        for path in CARTRIDGE_DIR.glob("*.json"):
            try:
                cart = json.loads(path.read_text())
                mastery_date = cart.get("mastery_date", "")
                if mastery_date:
                    try:
                        ts = datetime.fromisoformat(mastery_date).timestamp()
                        if ts < period_start:
                            continue
                    except (ValueError, TypeError):
                        pass

                stats["cartridge_relics"] += 1
                stats["unique_masteries"] += 1
                stats["total_achievements"] += cart.get("achievement_count", 0)
                stats["total_rtc"] += cart.get("total_rtc_earned", 0.0)

                platform = cart.get("platform", "")
                if platform:
                    platforms.add(platform)

                if cart.get("hardcore_mode", False):
                    stats["hardcore_masteries"] += 1
                if cart.get("first_press", False):
                    stats["first_press_count"] += 1
            except (json.JSONDecodeError, OSError):
                continue

        stats["platforms_played"] = sorted(platforms)
        stats["platform_variety_score"] = len(platforms) * 10

    # Hardcore rate
    if stats["unique_masteries"] > 0:
        stats["hardcore_rate"] = round(
            stats["hardcore_masteries"] / stats["unique_masteries"] * 100, 1
        )

    # Read session history for play time
    history_path = SESSIONS_DIR / "history.jsonl"
    if history_path.exists():
        try:
            games_set = set()
            for line in history_path.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    session = json.loads(line)
                    started_at = session.get("started_at", "")
                    if started_at:
                        try:
                            ts = datetime.fromisoformat(started_at).timestamp()
                            if ts < period_start:
                                continue
                        except (ValueError, TypeError):
                            pass

                    stats["total_session_minutes"] += session.get("duration_minutes", 0)
                    game_id = session.get("game_id")
                    if game_id:
                        games_set.add(game_id)
                except json.JSONDecodeError:
                    continue
            stats["games_played"] = len(games_set)
        except OSError:
            pass

    # Read system crowns
    crowns_path = STATE_DIR / "system_crowns.json"
    if crowns_path.exists():
        try:
            crowns = json.loads(crowns_path.read_text())
            if isinstance(crowns, list):
                stats["system_crowns"] = len(crowns)
        except (json.JSONDecodeError, OSError):
            pass

    # Read one-credit club
    occ_path = EVENTS_DIR / "one_credit_club.json"
    if occ_path.exists():
        try:
            occ = json.loads(occ_path.read_text())
            clears = occ.get("clears", [])
            count = 0
            for clear in clears:
                cleared_at = clear.get("cleared_at", "")
                if cleared_at:
                    try:
                        ts = datetime.fromisoformat(cleared_at).timestamp()
                        if ts >= period_start:
                            count += 1
                    except (ValueError, TypeError):
                        count += 1
                else:
                    count += 1
            stats["one_credit_clears"] = count
        except (json.JSONDecodeError, OSError):
            pass

    # Read season info
    season_path = EVENTS_DIR / "current_season.json"
    if season_path.exists():
        try:
            season = json.loads(season_path.read_text())
            stats["season_name"] = season.get("season_name", "")
        except (json.JSONDecodeError, OSError):
            pass

    stats["total_rtc"] = round(stats["total_rtc"], 6)
    stats["total_session_minutes"] = round(stats["total_session_minutes"], 1)

    return stats


# ---------------------------------------------------------------------------
# Network leaderboard
# ---------------------------------------------------------------------------

def fetch_network_leaderboard(config: Dict, sort_by: str = "rtc",
                               period: str = "all",
                               limit: int = 20) -> Optional[List[Dict]]:
    """Fetch global leaderboard from RustChain API.

    Expected API response format:
    GET /api/gaming/leaderboard?sort=rtc&period=weekly&limit=20
    {
        "leaderboard": [
            {
                "rank": 1,
                "wallet": "some-wallet-id",
                "total_rtc": 1.23456,
                "unique_masteries": 15,
                "platforms_played": 8,
                "platform_variety_score": 80,
                "hardcore_rate": 75.0,
                "season": "Season 2026Q1"
            },
            ...
        ],
        "total_participants": 42,
        "period": "weekly",
        "sort": "rtc",
        "as_of": "2026-03-21T00:00:00Z"
    }
    """
    node_url = config.get("rustchain", {}).get("node_url", "").rstrip("/")
    verify_ssl = config.get("rustchain", {}).get("verify_ssl", False)

    if not node_url:
        log.warning("No RustChain node URL configured")
        return None

    url = f"{node_url}/api/gaming/leaderboard"
    params = {
        "sort": sort_by,
        "period": period,
        "limit": limit,
    }

    try:
        resp = requests.get(url, params=params, verify=verify_ssl, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            entries = data.get("leaderboard", [])
            # Cache the result
            cache = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "sort": sort_by,
                "period": period,
                "data": data,
            }
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            LEADERBOARD_CACHE.write_text(json.dumps(cache, indent=2))
            return entries
        else:
            log.warning("Leaderboard API returned HTTP %d", resp.status_code)
    except requests.RequestException as e:
        log.warning("Could not fetch network leaderboard: %s", e)

    # Try cached data
    if LEADERBOARD_CACHE.exists():
        try:
            cache = json.loads(LEADERBOARD_CACHE.read_text())
            log.info("Using cached leaderboard from %s", cache.get("fetched_at", "unknown"))
            return cache.get("data", {}).get("leaderboard", [])
        except (json.JSONDecodeError, OSError):
            pass

    return None


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------

def format_local_leaderboard(stats: Dict) -> str:
    """Format local stats as a leaderboard display."""
    hours = int(stats["total_session_minutes"] // 60)
    mins = int(stats["total_session_minutes"] % 60)
    session_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"

    platforms_str = ", ".join(stats["platforms_played"][:5])
    if len(stats["platforms_played"]) > 5:
        platforms_str += f" +{len(stats['platforms_played']) - 5} more"
    if not platforms_str:
        platforms_str = "None yet"

    season_line = ""
    if stats["season_name"]:
        season_line = f"\n  Season: {stats['season_name']}"

    output = (
        f"\n"
        f"  +{'=' * 50}+\n"
        f"  |{'YOUR STATS':^50s}|\n"
        f"  |{stats['period_label']:^50s}|\n"
        f"  +{'=' * 50}+\n"
        f"  |  Wallet: {stats['wallet']:<38s}|\n"
        f"  +{'-' * 50}+\n"
        f"  |  Total RTC Earned:     {stats['total_rtc']:<24.5f}|\n"
        f"  |  Unique Masteries:     {stats['unique_masteries']:<24d}|\n"
        f"  |  Total Achievements:   {stats['total_achievements']:<24d}|\n"
        f"  |  Hardcore Masteries:   {stats['hardcore_masteries']:<24d}|\n"
        f"  |  Hardcore Rate:        {stats['hardcore_rate']:<23.1f}%|\n"
        f"  |  Platform Variety:     {stats['platform_variety_score']:<24d}|\n"
        f"  |  Platforms Played:     {len(stats['platforms_played']):<24d}|\n"
        f"  |  Games Played:         {stats['games_played']:<24d}|\n"
        f"  |  Session Time:         {session_str:<24s}|\n"
        f"  |  Cartridge Relics:     {stats['cartridge_relics']:<24d}|\n"
        f"  |  System Crowns:        {stats['system_crowns']:<24d}|\n"
        f"  |  First Press Relics:   {stats['first_press_count']:<24d}|\n"
        f"  |  One-Credit Clears:    {stats['one_credit_clears']:<24d}|\n"
        f"  +{'=' * 50}+\n"
        f"  Platforms: {platforms_str}"
        f"{season_line}\n"
    )
    return output


def format_network_leaderboard(entries: List[Dict], sort_by: str,
                                period: str, my_wallet: str) -> str:
    """Format network leaderboard entries for display."""
    if not entries:
        return "\n  No leaderboard data available.\n"

    sort_labels = {
        "rtc": "Total RTC",
        "masteries": "Unique Masteries",
        "variety": "Platform Variety",
        "hardcore": "Hardcore Rate",
    }
    sort_label = sort_labels.get(sort_by, sort_by.title())

    header = (
        f"\n"
        f"  +{'=' * 68}+\n"
        f"  |{'NETWORK LEADERBOARD':^68s}|\n"
        f"  |{f'Sorted by: {sort_label} | Period: {period_label(period)}':^68s}|\n"
        f"  +{'=' * 68}+\n"
        f"  | {'#':>3s} | {'Wallet':<24s} | {'RTC':>10s} | {'Mast':>4s} | {'Var':>3s} | {'HC%':>5s} |\n"
        f"  +{'-' * 68}+\n"
    )

    rows = []
    for entry in entries:
        rank = entry.get("rank", "?")
        wallet = entry.get("wallet", "unknown")
        rtc = entry.get("total_rtc", 0.0)
        masteries = entry.get("unique_masteries", 0)
        variety = entry.get("platform_variety_score", 0)
        hc_rate = entry.get("hardcore_rate", 0.0)

        # Truncate wallet for display
        wallet_display = wallet[:22] + ".." if len(wallet) > 24 else wallet

        # Highlight current player
        marker = " <--" if wallet == my_wallet else ""

        row = (
            f"  | {rank:>3} | {wallet_display:<24s} | "
            f"{rtc:>10.5f} | {masteries:>4d} | {variety:>3d} | "
            f"{hc_rate:>4.1f}% |{marker}\n"
        )
        rows.append(row)

    footer = f"  +{'=' * 68}+\n"

    return header + "".join(rows) + footer


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
        return {
            "rustchain": {"node_url": "https://50.28.86.131", "verify_ssl": False},
            "node_id": "rustchain-arcade-rpi",
        }


def main():
    parser = argparse.ArgumentParser(
        description="Sophia Edge Node -- Leaderboard"
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Show local stats (your own rankings)"
    )
    parser.add_argument(
        "--network", action="store_true",
        help="Show network leaderboard from RustChain API"
    )
    parser.add_argument(
        "--period", choices=["weekly", "monthly", "season", "all"],
        default="all",
        help="Time period for rankings (default: all)"
    )
    parser.add_argument(
        "--sort", choices=["rtc", "masteries", "variety", "hardcore"],
        default="rtc",
        help="Sort network leaderboard by (default: rtc)"
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="Number of entries for network leaderboard (default: 20)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of formatted text"
    )
    args = parser.parse_args()

    config = load_config()

    # Default to local if neither specified
    if not args.local and not args.network:
        args.local = True

    if args.local:
        stats = collect_local_stats(period=args.period)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print(format_local_leaderboard(stats))

    if args.network:
        my_wallet = os.environ.get(
            "SOPHIA_WALLET", config.get("node_id", "rustchain-arcade-rpi")
        )
        entries = fetch_network_leaderboard(
            config, sort_by=args.sort, period=args.period, limit=args.limit
        )
        if entries is not None:
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                print(format_network_leaderboard(
                    entries, args.sort, args.period, my_wallet
                ))
        else:
            print("\n  Could not fetch network leaderboard.")
            print("  Check your connection and RustChain node URL.\n")


if __name__ == "__main__":
    main()
