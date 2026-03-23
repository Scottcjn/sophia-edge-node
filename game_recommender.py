#!/usr/bin/env python3
"""
rustchain-arcade: Achievement-Optimized Game Recommender

Recommends games based on potential RTC earnings from RetroAchievements.
Factors in achievement count, rarity, hardcore completion rates, and
player's existing progress to suggest high-value games.

CLI usage:
    python3 game_recommender.py --platform snes --sort rtc_potential
    python3 game_recommender.py --near-mastery
    python3 game_recommender.py --hidden-gems --platform genesis
    python3 game_recommender.py --installed-only
"""

import argparse
import json
import logging
import os
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
log = logging.getLogger("sophia-recommender")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get(
    "SOPHIA_CONFIG", "/opt/rustchain-arcade/config.json"
)
STATE_DIR = Path.home() / ".rustchain-arcade"
CARTRIDGE_DIR = STATE_DIR / "cartridges"
RECOMMENDER_CACHE = STATE_DIR / "recommender_cache.json"

# ---------------------------------------------------------------------------
# RetroAchievements platform IDs (console_id -> name mapping)
# ---------------------------------------------------------------------------
PLATFORM_IDS = {
    "nes": 7,
    "snes": 3,
    "genesis": 1,
    "mega drive": 1,
    "n64": 2,
    "gb": 4,
    "game boy": 4,
    "gbc": 6,
    "game boy color": 6,
    "gba": 5,
    "game boy advance": 5,
    "ps1": 12,
    "playstation": 12,
    "atari2600": 25,
    "atari 2600": 25,
    "atari7800": 51,
    "atari 7800": 51,
    "sms": 11,
    "master system": 11,
    "gg": 15,
    "game gear": 15,
    "tg16": 8,
    "turbografx-16": 8,
    "pce": 8,
    "pc engine": 8,
    "saturn": 39,
    "dreamcast": 40,
    "nds": 18,
    "nintendo ds": 18,
    "psp": 41,
    "arcade": 27,
    "lynx": 13,
    "wonderswan": 53,
    "colecovision": 44,
    "sg1000": 33,
    "32x": 10,
    "virtual boy": 28,
    "neo geo": 14,
    "msx": 29,
    "vectrex": 46,
    "intellivision": 45,
}

# Reverse mapping: console_id -> canonical name
CONSOLE_NAMES = {
    7: "NES", 3: "SNES", 1: "Genesis", 2: "N64",
    4: "Game Boy", 6: "Game Boy Color", 5: "Game Boy Advance",
    12: "PlayStation", 25: "Atari 2600", 51: "Atari 7800",
    11: "Master System", 15: "Game Gear", 8: "TurboGrafx-16",
    39: "Saturn", 40: "Dreamcast", 18: "Nintendo DS",
    41: "PSP", 27: "Arcade", 13: "Lynx", 53: "WonderSwan",
    44: "ColecoVision", 33: "SG-1000", 10: "32X",
    28: "Virtual Boy", 14: "Neo Geo", 29: "MSX",
    46: "Vectrex", 45: "Intellivision",
}


# ---------------------------------------------------------------------------
# RetroAchievements API client
# ---------------------------------------------------------------------------

class RAClient:
    """Lightweight RetroAchievements API client for game discovery."""

    def __init__(self, api_url: str, username: str, api_key: str):
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "rustchain-arcade/2.0"

    def _get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make authenticated GET request to RA API."""
        params = params or {}
        params["z"] = self.username
        params["y"] = self.api_key
        url = f"{self.api_url}/{endpoint}"

        try:
            resp = self.session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("RA API error: %s", e)
            return None

    def get_console_games(self, console_id: int) -> Optional[List[Dict]]:
        """Fetch game list for a console.

        GET /API_GetGameList.php?z={}&y={}&i={console_id}&h=1&f=1
        h=1: include hashes, f=1: only with achievements
        """
        data = self._get("API_GetGameList.php", {
            "i": console_id,
            "h": 0,
            "f": 1,  # Only games with achievements
        })
        if isinstance(data, list):
            return data
        return None

    def get_game_info_and_progress(self, game_id: int) -> Optional[Dict]:
        """Fetch detailed game info with user progress.

        GET /API_GetGameInfoAndUserProgress.php?z={}&y={}&u={}&g={game_id}
        """
        return self._get("API_GetGameInfoAndUserProgress.php", {
            "u": self.username,
            "g": game_id,
        })

    def get_user_completed_games(self) -> Optional[List[Dict]]:
        """Fetch user's completed/mastered games.

        GET /API_GetUserCompletedGames.php?z={}&y={}&u={}
        """
        data = self._get("API_GetUserCompletedGames.php", {
            "u": self.username,
        })
        if isinstance(data, list):
            return data
        return None

    def get_user_recently_played(self, count: int = 50) -> Optional[List[Dict]]:
        """Fetch user's recently played games.

        GET /API_GetUserRecentlyPlayedGames.php?z={}&y={}&u={}&c={}
        """
        data = self._get("API_GetUserRecentlyPlayedGames.php", {
            "u": self.username,
            "c": count,
        })
        if isinstance(data, list):
            return data
        return None


# ---------------------------------------------------------------------------
# RTC potential calculation
# ---------------------------------------------------------------------------

def estimate_rtc_potential(game: Dict, config: Dict) -> float:
    """Estimate total RTC earnable from a game.

    Factors:
      - Number of achievements
      - Average point value
      - Estimated rarity (from player count)
      - Hardcore multiplier
      - User's existing progress (subtract already-earned)
    """
    tiers = config.get("achievements", {}).get("reward_tiers", {})
    rarity_cfg = config.get("rarity_factors", {})
    hc_mult = config.get("achievements", {}).get("hardcore_multiplier", 2.0)

    num_achievements = int(game.get("NumAchievements", 0))
    num_players = int(game.get("NumDistinctPlayers", game.get("NumDistinctPlayersCasual", 1)))
    points_total = int(game.get("points_total", 0))
    user_awarded = int(game.get("NumAwardedToUser", 0))
    remaining = num_achievements - user_awarded

    if remaining <= 0 or num_achievements == 0:
        return 0.0

    # Average points per achievement
    avg_points = points_total / num_achievements if num_achievements > 0 else 5

    # Estimate tier from average points
    if avg_points >= 50:
        base_rtc = tiers.get("legendary", {}).get("rtc", 0.005)
    elif avg_points >= 25:
        base_rtc = tiers.get("ultra_rare", {}).get("rtc", 0.001)
    elif avg_points >= 10:
        base_rtc = tiers.get("rare", {}).get("rtc", 0.0005)
    elif avg_points >= 5:
        base_rtc = tiers.get("uncommon", {}).get("rtc", 0.0002)
    else:
        base_rtc = tiers.get("common", {}).get("rtc", 0.00005)

    # Estimate rarity factor from player count
    # Fewer players = higher rarity
    if num_players < 50:
        rarity = rarity_cfg.get("legendary_below_1pct", 3.0)
    elif num_players < 200:
        rarity = rarity_cfg.get("ultra_rare_1_5pct", 2.5)
    elif num_players < 1000:
        rarity = rarity_cfg.get("rare_5_20pct", 1.75)
    elif num_players < 5000:
        rarity = rarity_cfg.get("uncommon_20_50pct", 1.25)
    else:
        rarity = rarity_cfg.get("common_above_50pct", 1.0)

    # Total RTC for remaining achievements (assuming hardcore)
    total_rtc = remaining * base_rtc * rarity * hc_mult

    # Add mastery bonus if close to completion
    mastery_cfg = config.get("mastery_milestones", {})
    if user_awarded > 0:  # Has started the game
        total_rtc += mastery_cfg.get("full_mastery", 0.02)
        # If would be legendary mastery (all hardcore)
        total_rtc += mastery_cfg.get("legendary_mastery", 0.05)

    return round(total_rtc, 6)


def estimate_time_hours(game: Dict) -> float:
    """Rough estimate of hours needed to complete remaining achievements.

    Uses achievement count as proxy: ~2 min per achievement average.
    """
    remaining = int(game.get("NumAchievements", 0)) - int(game.get("NumAwardedToUser", 0))
    if remaining <= 0:
        return 0.0
    # Rough estimate: 2-5 minutes per achievement on average
    return round(remaining * 3.0 / 60.0, 1)


def rtc_per_hour(rtc_potential: float, estimated_hours: float) -> float:
    """Calculate RTC earned per hour of play."""
    if estimated_hours <= 0:
        return 0.0
    return round(rtc_potential / estimated_hours, 6)


# ---------------------------------------------------------------------------
# Game filtering and recommendation
# ---------------------------------------------------------------------------

def get_mastered_game_ids() -> set:
    """Get set of game IDs already mastered (from cartridge wallet)."""
    mastered = set()
    if CARTRIDGE_DIR.exists():
        for path in CARTRIDGE_DIR.glob("*.json"):
            try:
                cart = json.loads(path.read_text())
                game_id = cart.get("game_id")
                if game_id is not None:
                    mastered.add(int(game_id))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
    return mastered


def get_installed_cores() -> set:
    """Detect installed RetroArch cores to filter recommendations.

    Checks common RetroArch core directories.
    """
    core_dirs = [
        Path.home() / ".config" / "retroarch" / "cores",
        Path("/usr/lib/libretro"),
        Path("/usr/lib/arm-linux-gnueabihf/libretro"),
        Path("/usr/lib/aarch64-linux-gnu/libretro"),
        Path("/opt/retropie/libretrocores"),
    ]

    # Core name to platform mapping
    core_platforms = {
        "fceumm": "nes", "nestopia": "nes", "mesen": "nes",
        "snes9x": "snes", "bsnes": "snes",
        "genesis_plus_gx": "genesis", "picodrive": "genesis",
        "mupen64plus": "n64", "parallel_n64": "n64",
        "gambatte": "gb", "mgba": "gba", "vbam": "gba",
        "pcsx_rearmed": "playstation", "beetle_psx": "playstation",
        "stella": "atari2600", "prosystem": "atari7800",
        "smsplus": "master system", "gearsystem": "master system",
        "mednafen_pce": "tg16", "beetle_pce": "tg16",
        "mednafen_saturn": "saturn", "beetle_saturn": "saturn",
        "flycast": "dreamcast", "reicast": "dreamcast",
        "desmume": "nds", "melonds": "nds",
        "ppsspp": "psp",
        "fbneo": "arcade", "mame": "arcade",
        "handy": "lynx",
        "mednafen_wswan": "wonderswan",
        "gearcoleco": "colecovision",
        "mednafen_vb": "virtual boy",
        "vecx": "vectrex",
    }

    installed_platforms = set()
    for core_dir in core_dirs:
        if not core_dir.exists():
            continue
        for entry in core_dir.iterdir():
            core_name = entry.stem.lower()
            # Strip _libretro suffix
            if core_name.endswith("_libretro"):
                core_name = core_name[:-9]
            platform = core_platforms.get(core_name)
            if platform:
                installed_platforms.add(platform)

    return installed_platforms


def filter_near_mastery(games: List[Dict], threshold: float = 80.0) -> List[Dict]:
    """Filter games that are close to mastery (>threshold% complete)."""
    near = []
    for game in games:
        total = int(game.get("NumAchievements", 0))
        awarded = int(game.get("NumAwardedToUser", 0))
        if total > 0 and awarded > 0:
            pct = (awarded / total) * 100.0
            if pct >= threshold and pct < 100.0:
                game["completion_pct"] = round(pct, 1)
                game["remaining_achievements"] = total - awarded
                near.append(game)
    return near


def filter_hidden_gems(games: List[Dict], max_players: int = 200) -> List[Dict]:
    """Filter 'hidden gems': games with few players but good achievement sets.

    These have high rarity factor = more RTC per achievement.
    """
    gems = []
    for game in games:
        num_players = int(game.get("NumDistinctPlayers",
                                    game.get("NumDistinctPlayersCasual", 0)))
        num_achievements = int(game.get("NumAchievements", 0))
        if 0 < num_players <= max_players and num_achievements >= 10:
            game["player_count"] = num_players
            gems.append(game)
    return gems


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------

def generate_recommendations(client: RAClient, config: Dict,
                              platform: Optional[str] = None,
                              sort_by: str = "rtc_potential",
                              near_mastery: bool = False,
                              hidden_gems: bool = False,
                              installed_only: bool = False,
                              limit: int = 15) -> List[Dict]:
    """Generate game recommendations with RTC potential estimates.

    Returns a sorted list of recommended games.
    """
    mastered = get_mastered_game_ids()
    installed_platforms = get_installed_cores() if installed_only else set()

    # Determine which platforms to query
    platforms_to_query = []
    if platform:
        platform_lower = platform.lower()
        console_id = PLATFORM_IDS.get(platform_lower)
        if console_id:
            platforms_to_query.append((console_id, platform_lower))
        else:
            log.error("Unknown platform: %s", platform)
            log.info("Available: %s", ", ".join(sorted(PLATFORM_IDS.keys())))
            return []
    elif installed_only and installed_platforms:
        for plat_name in installed_platforms:
            cid = PLATFORM_IDS.get(plat_name)
            if cid:
                platforms_to_query.append((cid, plat_name))
    else:
        # Default: popular platforms
        for plat_name in ["snes", "nes", "genesis", "gba", "gb", "n64", "playstation"]:
            cid = PLATFORM_IDS.get(plat_name)
            if cid:
                platforms_to_query.append((cid, plat_name))

    all_games = []

    for console_id, plat_name in platforms_to_query:
        log.info("Fetching games for %s (console_id=%d)...",
                 CONSOLE_NAMES.get(console_id, plat_name), console_id)

        # Check cache first
        cache_key = f"console_{console_id}"
        cached_games = _load_cache(cache_key)
        if cached_games is not None:
            games = cached_games
            log.info("  Using cached data (%d games)", len(games))
        else:
            games = client.get_console_games(console_id)
            if games is None:
                log.warning("  Failed to fetch game list")
                continue
            _save_cache(cache_key, games)
            log.info("  Found %d games with achievements", len(games))
            # Rate limit: be nice to RA API
            time.sleep(1.0)

        for game in games:
            game_id = int(game.get("ID", 0))
            if game_id in mastered:
                continue  # Already mastered

            game["console_id"] = console_id
            game["console_name"] = CONSOLE_NAMES.get(console_id, plat_name)
            game["points_total"] = int(game.get("points_total",
                                                  game.get("Points", 0)))
            all_games.append(game)

    if not all_games:
        log.info("No unmastered games found for the selected platform(s)")
        return []

    # For near-mastery mode, we need user progress data
    if near_mastery:
        log.info("Fetching user progress for near-mastery filter...")
        recently_played = client.get_user_recently_played(count=100)
        if recently_played:
            progress_map = {}
            for rp in recently_played:
                gid = int(rp.get("GameID", 0))
                progress_map[gid] = {
                    "NumAwardedToUser": int(rp.get("NumAchieved", 0)),
                    "NumAchievements": int(rp.get("NumPossibleAchievements",
                                                    rp.get("AchievementsTotal", 0))),
                }

            # Enrich games with progress
            for game in all_games:
                gid = int(game.get("ID", 0))
                if gid in progress_map:
                    game.update(progress_map[gid])

            all_games = filter_near_mastery(all_games)
            log.info("Found %d games near mastery (>80%%)", len(all_games))

    # Hidden gems filter
    if hidden_gems:
        all_games = filter_hidden_gems(all_games)
        log.info("Found %d hidden gems", len(all_games))

    # Calculate RTC potential for each game
    for game in all_games:
        game["rtc_potential"] = estimate_rtc_potential(game, config)
        game["estimated_hours"] = estimate_time_hours(game)
        game["rtc_per_hour"] = rtc_per_hour(game["rtc_potential"], game["estimated_hours"])

    # Sort
    sort_keys = {
        "rtc_potential": lambda g: g.get("rtc_potential", 0),
        "rtc_per_hour": lambda g: g.get("rtc_per_hour", 0),
        "achievements": lambda g: int(g.get("NumAchievements", 0)),
        "rarity": lambda g: -int(g.get("NumDistinctPlayers",
                                        g.get("NumDistinctPlayersCasual", 99999))),
        "completion": lambda g: g.get("completion_pct", 0),
    }
    sort_fn = sort_keys.get(sort_by, sort_keys["rtc_potential"])
    all_games.sort(key=sort_fn, reverse=True)

    return all_games[:limit]


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

def _load_cache(key: str) -> Optional[List]:
    """Load cached data if fresh (< 24 hours old)."""
    if not RECOMMENDER_CACHE.exists():
        return None
    try:
        cache = json.loads(RECOMMENDER_CACHE.read_text())
        entry = cache.get(key)
        if entry and time.time() - entry.get("timestamp", 0) < 86400:
            return entry.get("data")
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(key: str, data: List) -> None:
    """Save data to cache."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cache = {}
    if RECOMMENDER_CACHE.exists():
        try:
            cache = json.loads(RECOMMENDER_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    cache[key] = {"timestamp": time.time(), "data": data}
    RECOMMENDER_CACHE.write_text(json.dumps(cache))


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def display_recommendations(games: List[Dict], sort_by: str,
                             near_mastery: bool, hidden_gems: bool) -> None:
    """Print recommendations in a formatted table."""
    if not games:
        print("\n  No recommendations found. Try a different platform or filter.\n")
        return

    mode = "Near Mastery" if near_mastery else ("Hidden Gems" if hidden_gems else "Top RTC Potential")
    print(f"\n  === Game Recommendations: {mode} ===\n")

    # Header
    if near_mastery:
        print(f"  {'#':>3s}  {'Game':<30s}  {'Platform':<8s}  {'Done':>5s}  {'Left':>4s}  {'RTC':>10s}")
        print(f"  {'---':>3s}  {'-' * 30:<30s}  {'-' * 8:<8s}  {'-----':>5s}  {'----':>4s}  {'-' * 10:>10s}")
    else:
        print(f"  {'#':>3s}  {'Game':<30s}  {'Platform':<8s}  {'Ach':>4s}  {'RTC Pot':>10s}  {'RTC/hr':>10s}")
        print(f"  {'---':>3s}  {'-' * 30:<30s}  {'-' * 8:<8s}  {'----':>4s}  {'-' * 10:>10s}  {'-' * 10:>10s}")

    for i, game in enumerate(games):
        title = game.get("Title", "Unknown")
        if len(title) > 28:
            title = title[:25] + "..."
        console = game.get("console_name", "?")[:8]
        num_ach = int(game.get("NumAchievements", 0))

        if near_mastery:
            pct = game.get("completion_pct", 0)
            remaining = game.get("remaining_achievements", 0)
            rtc = game.get("rtc_potential", 0)
            print(f"  {i + 1:>3d}  {title:<30s}  {console:<8s}  {pct:>4.1f}%  {remaining:>4d}  {rtc:>10.5f}")
        else:
            rtc_pot = game.get("rtc_potential", 0)
            rtc_hr = game.get("rtc_per_hour", 0)
            print(f"  {i + 1:>3d}  {title:<30s}  {console:<8s}  {num_ach:>4d}  {rtc_pot:>10.5f}  {rtc_hr:>10.6f}")

    print()

    # Tips
    if hidden_gems:
        print("  Tip: Hidden gems have fewer players = higher rarity = more RTC per achievement!")
    elif near_mastery:
        print("  Tip: Completing these games earns mastery bonus + cartridge relic!")
    else:
        print("  Tip: Use --hidden-gems for rare games or --near-mastery for easy wins")
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
        log.warning("Config not found at %s, using defaults", cfg_path)
        return {
            "achievements": {
                "retroachievements": {"api_url": "https://retroachievements.org/API"},
                "reward_tiers": {
                    "common": {"min_points": 1, "max_points": 5, "rtc": 0.00005},
                    "uncommon": {"min_points": 5, "max_points": 10, "rtc": 0.0002},
                    "rare": {"min_points": 10, "max_points": 25, "rtc": 0.0005},
                    "ultra_rare": {"min_points": 25, "max_points": 50, "rtc": 0.001},
                    "legendary": {"min_points": 50, "max_points": 100, "rtc": 0.005},
                },
                "hardcore_multiplier": 2.0,
            },
            "rarity_factors": {
                "common_above_50pct": 1.0,
                "uncommon_20_50pct": 1.25,
                "rare_5_20pct": 1.75,
                "ultra_rare_1_5pct": 2.5,
                "legendary_below_1pct": 3.0,
            },
            "mastery_milestones": {
                "full_mastery": 0.02,
                "legendary_mastery": 0.05,
            },
        }


def main():
    parser = argparse.ArgumentParser(
        description="Sophia Edge Node -- Game Recommender"
    )
    parser.add_argument(
        "--platform", type=str, default=None,
        help="Filter by platform (e.g., snes, nes, genesis, gba, n64)"
    )
    parser.add_argument(
        "--sort", choices=["rtc_potential", "rtc_per_hour", "achievements", "rarity", "completion"],
        default="rtc_potential",
        help="Sort recommendations by (default: rtc_potential)"
    )
    parser.add_argument(
        "--near-mastery", action="store_true",
        help="Show games >80%% complete (easy mastery targets)"
    )
    parser.add_argument(
        "--hidden-gems", action="store_true",
        help="Show games with few players but good achievement sets"
    )
    parser.add_argument(
        "--installed-only", action="store_true",
        help="Only recommend games for installed RetroArch cores"
    )
    parser.add_argument(
        "--limit", type=int, default=15,
        help="Number of recommendations (default: 15)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON"
    )
    parser.add_argument(
        "--list-platforms", action="store_true",
        help="List available platform names"
    )
    args = parser.parse_args()

    if args.list_platforms:
        print("\n  Available platforms:")
        seen = set()
        for name, cid in sorted(PLATFORM_IDS.items()):
            canonical = CONSOLE_NAMES.get(cid, name)
            if canonical not in seen:
                print(f"    {name:<20s} ({canonical})")
                seen.add(canonical)
        print()
        return

    config = load_config()

    # Get RA credentials
    ra_cfg = config.get("achievements", {}).get("retroachievements", {})
    username = os.environ.get("RA_USERNAME", ra_cfg.get("username", ""))
    api_key = os.environ.get("RA_API_KEY", ra_cfg.get("api_key", ""))

    if not username or not api_key:
        print("\n  RetroAchievements credentials required.")
        print("  Set RA_USERNAME and RA_API_KEY environment variables,")
        print("  or configure in config.json.\n")
        sys.exit(1)

    client = RAClient(
        api_url=ra_cfg.get("api_url", "https://retroachievements.org/API"),
        username=username,
        api_key=api_key,
    )

    recommendations = generate_recommendations(
        client=client,
        config=config,
        platform=args.platform,
        sort_by=args.sort,
        near_mastery=args.near_mastery,
        hidden_gems=args.hidden_gems,
        installed_only=args.installed_only,
        limit=args.limit,
    )

    if args.json:
        # Clean output for JSON (remove non-serializable items)
        output = []
        for game in recommendations:
            output.append({
                "game_id": game.get("ID"),
                "title": game.get("Title"),
                "console": game.get("console_name"),
                "num_achievements": int(game.get("NumAchievements", 0)),
                "rtc_potential": game.get("rtc_potential", 0),
                "rtc_per_hour": game.get("rtc_per_hour", 0),
                "estimated_hours": game.get("estimated_hours", 0),
                "completion_pct": game.get("completion_pct"),
                "player_count": game.get("player_count",
                                          game.get("NumDistinctPlayers")),
            })
        print(json.dumps(output, indent=2))
    else:
        display_recommendations(
            recommendations, args.sort, args.near_mastery, args.hidden_gems
        )


if __name__ == "__main__":
    main()
