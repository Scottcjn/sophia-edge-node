#!/usr/bin/env python3
"""
rustchain-arcade: Cartridge Wallet -- Soulbound Collectible System

Each mastered game becomes a "cartridge relic" stored locally, a soulbound
digital collectible that records your achievement. Not tradeable, not fungible --
just pure bragging rights.

Features:
  - Cartridge relics for mastered games with ASCII art labels
  - Memory Card profile summary (all cartridges, stats, badges)
  - System Crown tracking (5 masteries on one platform)
  - First Press detection (first on RustChain to master a game)
  - Export profile as JSON for sharing/verification
"""

import hashlib
import json
import logging
import os
import textwrap
import time
from datetime import datetime, timezone
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
log = logging.getLogger("sophia-cartridge-wallet")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
STATE_DIR = Path.home() / ".rustchain-arcade"
CARTRIDGE_DIR = STATE_DIR / "cartridges"
PROFILE_PATH = STATE_DIR / "memory_card.json"
CROWNS_PATH = STATE_DIR / "system_crowns.json"

# ---------------------------------------------------------------------------
# Platform art templates
# ---------------------------------------------------------------------------

PLATFORM_ART = {
    "nes": "NES",
    "snes": "SNES",
    "super nintendo": "SNES",
    "genesis": "GEN",
    "mega drive": "GEN",
    "game boy": "GB",
    "game boy advance": "GBA",
    "game boy color": "GBC",
    "nintendo 64": "N64",
    "playstation": "PS1",
    "atari 2600": "2600",
    "atari 7800": "7800",
    "master system": "SMS",
    "game gear": "GG",
    "turbografx-16": "TG16",
    "pc engine": "TG16",
    "neo geo": "NEO",
    "saturn": "SAT",
    "dreamcast": "DC",
    "nintendo ds": "NDS",
    "psp": "PSP",
    "arcade": "ARC",
    "lynx": "LYNX",
    "wonderswan": "WS",
    "colecovision": "COLV",
    "intellivision": "INTV",
    "virtual boy": "VB",
    "sg-1000": "SG1K",
    "32x": "32X",
}


def _platform_tag(platform: str) -> str:
    """Get short platform tag for ASCII art."""
    lower = platform.lower()
    for key, tag in PLATFORM_ART.items():
        if key in lower:
            return tag
    return platform[:4].upper()


# ---------------------------------------------------------------------------
# ASCII art cartridge generator
# ---------------------------------------------------------------------------

def generate_cartridge_art(
    game_title: str,
    platform: str,
    mastery_date: str,
    achievement_count: int,
    hardcore: bool,
    total_rtc: float,
    rarity_badges: List[str],
    first_press: bool = False,
) -> str:
    """Generate ASCII art cartridge label for a mastered game.

    Returns a multi-line string with the cartridge art.
    """
    ptag = _platform_tag(platform)
    mode = "HARDCORE" if hardcore else "STANDARD"
    fp_mark = " *FIRST PRESS*" if first_press else ""

    # Truncate title to fit in the label (max 28 chars)
    title_display = game_title[:28]
    if len(game_title) > 28:
        title_display = game_title[:25] + "..."

    # Badge line
    badge_line = " ".join(f"[{b}]" for b in rarity_badges[:3]) if rarity_badges else "[MASTERED]"
    if len(badge_line) > 30:
        badge_line = badge_line[:27] + "..."

    art = f"""\
    ╔════════════════════════════════╗
    ║  ┌────────────────────────┐   ║
    ║  │ {title_display:<26s} │   ║
    ║  │                          │   ║
    ║  │  Platform: {ptag:<14s} │   ║
    ║  │  Mode:     {mode:<14s} │   ║
    ║  │  Cheevos:  {achievement_count:<14d} │   ║
    ║  │  RTC:      {total_rtc:<14.5f} │   ║
    ║  │                          │   ║
    ║  │  {badge_line:<26s} │   ║
    ║  └────────────────────────┘   ║
    ║                                ║
    ║  Mastered: {mastery_date:<20s} ║
    ║  {fp_mark:<32s}║
    ╚════════════════════════════════╝"""

    return art


# ---------------------------------------------------------------------------
# CartridgeWallet class
# ---------------------------------------------------------------------------

class CartridgeWallet:
    """Manage the local collection of cartridge relics."""

    def __init__(self):
        CARTRIDGE_DIR.mkdir(parents=True, exist_ok=True)

    def mint_cartridge(
        self,
        game_id: int,
        game_title: str,
        platform: str,
        achievement_count: int,
        hardcore: bool = False,
        total_rtc_earned: float = 0.0,
        rarity_badges: Optional[List[str]] = None,
        first_press: bool = False,
    ) -> Dict:
        """Create a new cartridge relic for a mastered game.

        Returns the cartridge data dict.
        """
        if rarity_badges is None:
            rarity_badges = []

        now = datetime.now(timezone.utc)
        mastery_date = now.strftime("%Y-%m-%d %H:%M UTC")

        # Generate unique cartridge ID
        cart_seed = f"{game_id}:{game_title}:{now.isoformat()}"
        cartridge_id = hashlib.sha256(cart_seed.encode()).hexdigest()[:16]

        # Generate ASCII art
        art = generate_cartridge_art(
            game_title=game_title,
            platform=platform,
            mastery_date=mastery_date,
            achievement_count=achievement_count,
            hardcore=hardcore,
            total_rtc=total_rtc_earned,
            rarity_badges=rarity_badges,
            first_press=first_press,
        )

        cartridge = {
            "cartridge_id": cartridge_id,
            "game_id": game_id,
            "game_title": game_title,
            "platform": platform,
            "mastery_date": now.isoformat(),
            "mastery_date_display": mastery_date,
            "total_rtc_earned": total_rtc_earned,
            "achievement_count": achievement_count,
            "hardcore_mode": hardcore,
            "rarity_badges": rarity_badges,
            "first_press": first_press,
            "ascii_art": art,
            "minted_at": now.isoformat(),
            "wallet": os.environ.get("SOPHIA_WALLET", "unknown"),
        }

        # Save to disk
        cart_path = CARTRIDGE_DIR / f"{game_id}.json"
        cart_path.write_text(json.dumps(cartridge, indent=2))

        # Also save the ASCII art as a separate text file
        art_path = CARTRIDGE_DIR / f"{game_id}.txt"
        art_path.write_text(art + "\n")

        log.info("Cartridge minted: %s (%s) -- %s", game_title, platform,
                 "FIRST PRESS!" if first_press else "collected")

        # Update profile
        self._update_profile()

        return cartridge

    def get_cartridge(self, game_id: int) -> Optional[Dict]:
        """Load a cartridge relic by game ID."""
        cart_path = CARTRIDGE_DIR / f"{game_id}.json"
        if cart_path.exists():
            try:
                return json.loads(cart_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def list_cartridges(self) -> List[Dict]:
        """List all cartridge relics, sorted by mastery date."""
        carts = []
        for path in CARTRIDGE_DIR.glob("*.json"):
            if path.name == "memory_card.json":
                continue
            try:
                cart = json.loads(path.read_text())
                carts.append(cart)
            except (json.JSONDecodeError, OSError):
                continue

        carts.sort(key=lambda c: c.get("mastery_date", ""), reverse=True)
        return carts

    def get_platform_masteries(self) -> Dict[str, int]:
        """Count masteries per platform."""
        platform_counts: Dict[str, int] = {}
        for cart in self.list_cartridges():
            plat = cart.get("platform", "Unknown")
            platform_counts[plat] = platform_counts.get(plat, 0) + 1
        return platform_counts

    def check_system_crowns(self) -> List[Dict]:
        """Check for System Crowns (5+ masteries on one platform)."""
        crowns = []
        for platform, count in self.get_platform_masteries().items():
            if count >= 5:
                crowns.append({
                    "platform": platform,
                    "mastery_count": count,
                    "earned_at": datetime.now(timezone.utc).isoformat(),
                })
        return crowns

    def get_favorite_system(self) -> Optional[str]:
        """Return platform with most masteries."""
        counts = self.get_platform_masteries()
        if not counts:
            return None
        return max(counts, key=counts.get)

    def add_rtc_to_cartridge(self, game_id: int, rtc_amount: float) -> None:
        """Add RTC earned to an existing cartridge's running total."""
        cart = self.get_cartridge(game_id)
        if cart:
            cart["total_rtc_earned"] = cart.get("total_rtc_earned", 0.0) + rtc_amount
            cart_path = CARTRIDGE_DIR / f"{game_id}.json"
            cart_path.write_text(json.dumps(cart, indent=2))

    def _update_profile(self) -> None:
        """Update the Memory Card profile summary."""
        carts = self.list_cartridges()
        platform_counts = self.get_platform_masteries()
        crowns = self.check_system_crowns()
        favorite = self.get_favorite_system()

        total_rtc = sum(c.get("total_rtc_earned", 0.0) for c in carts)
        total_achievements = sum(c.get("achievement_count", 0) for c in carts)
        hardcore_count = sum(1 for c in carts if c.get("hardcore_mode", False))
        first_press_count = sum(1 for c in carts if c.get("first_press", False))

        profile = {
            "wallet": os.environ.get("SOPHIA_WALLET", "unknown"),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "total_cartridges": len(carts),
            "total_achievements": total_achievements,
            "total_rtc_earned": round(total_rtc, 6),
            "hardcore_masteries": hardcore_count,
            "first_press_count": first_press_count,
            "favorite_system": favorite,
            "platform_masteries": platform_counts,
            "system_crowns": crowns,
            "authenticity_badges": [],
            "cartridge_ids": [c.get("cartridge_id") for c in carts],
        }

        PROFILE_PATH.write_text(json.dumps(profile, indent=2))

        # Save crowns separately
        if crowns:
            CROWNS_PATH.write_text(json.dumps(crowns, indent=2))

    def export_profile(self) -> Dict:
        """Export full profile as JSON for sharing/verification."""
        if PROFILE_PATH.exists():
            try:
                profile = json.loads(PROFILE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                self._update_profile()
                profile = json.loads(PROFILE_PATH.read_text())
        else:
            self._update_profile()
            profile = json.loads(PROFILE_PATH.read_text())

        # Include cartridge summaries (not full art, keep export compact)
        carts = self.list_cartridges()
        profile["cartridges"] = [
            {
                "game_id": c["game_id"],
                "game_title": c["game_title"],
                "platform": c["platform"],
                "mastery_date": c["mastery_date"],
                "hardcore": c.get("hardcore_mode", False),
                "achievements": c.get("achievement_count", 0),
                "rtc": c.get("total_rtc_earned", 0.0),
                "first_press": c.get("first_press", False),
            }
            for c in carts
        ]

        # Sign the profile for verification
        profile_str = json.dumps(profile, sort_keys=True)
        profile["verification_hash"] = hashlib.sha256(profile_str.encode()).hexdigest()[:32]

        return profile

    def print_collection(self) -> None:
        """Print all cartridge ASCII art to stdout."""
        carts = self.list_cartridges()
        if not carts:
            print("\n  No cartridges yet. Master a game to mint your first relic!\n")
            return

        print(f"\n  === MEMORY CARD === ({len(carts)} cartridges)\n")
        for cart in carts:
            print(cart.get("ascii_art", "[no art]"))
            print()

        # Print summary
        profile = self.export_profile()
        print(f"  Total Masteries: {profile['total_cartridges']}")
        print(f"  Total Achievements: {profile['total_achievements']}")
        print(f"  Total RTC Earned: {profile['total_rtc_earned']:.5f}")
        print(f"  Hardcore Masteries: {profile['hardcore_masteries']}")
        if profile.get("favorite_system"):
            print(f"  Favorite System: {profile['favorite_system']}")
        if profile.get("system_crowns"):
            for crown in profile["system_crowns"]:
                print(f"  System Crown: {crown['platform']} ({crown['mastery_count']} masteries)")
        if profile.get("first_press_count", 0) > 0:
            print(f"  First Press Relics: {profile['first_press_count']}")
        print()


def check_first_press(game_id: int, config: Dict) -> bool:
    """Check with the RustChain node if this is the first mastery of a game.

    Queries /api/gaming/first_press?game_id=X -- returns true if nobody else
    has claimed mastery for this game yet.

    Falls back to False (assume not first) on network errors.
    """
    node_url = config.get("rustchain", {}).get("node_url", "").rstrip("/")
    verify_ssl = config.get("rustchain", {}).get("verify_ssl", False)

    if not node_url:
        return False

    url = f"{node_url}/api/gaming/first_press"
    try:
        resp = requests.get(url, params={"game_id": game_id}, verify=verify_ssl, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("is_first", False)
    except (requests.RequestException, json.JSONDecodeError, KeyError):
        pass

    return False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    """CLI for viewing cartridge wallet."""
    import argparse
    parser = argparse.ArgumentParser(description="Sophia Edge Node -- Cartridge Wallet")
    parser.add_argument("--list", action="store_true", help="Show all cartridge relics")
    parser.add_argument("--export", action="store_true", help="Export profile as JSON")
    parser.add_argument("--show", type=int, metavar="GAME_ID", help="Show specific cartridge")
    parser.add_argument("--crowns", action="store_true", help="Show System Crowns")
    args = parser.parse_args()

    cw = CartridgeWallet()

    if args.export:
        profile = cw.export_profile()
        print(json.dumps(profile, indent=2))
    elif args.show:
        cart = cw.get_cartridge(args.show)
        if cart:
            print(cart.get("ascii_art", "[no art]"))
            print(json.dumps({k: v for k, v in cart.items() if k != "ascii_art"}, indent=2))
        else:
            print(f"No cartridge found for game ID {args.show}")
    elif args.crowns:
        crowns = cw.check_system_crowns()
        if crowns:
            for crown in crowns:
                print(f"  System Crown: {crown['platform']} ({crown['mastery_count']} masteries)")
        else:
            print("  No System Crowns yet. Master 5 games on one platform!")
    else:
        cw.print_collection()


if __name__ == "__main__":
    main()
