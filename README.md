# RustChain Arcade

**Small RTC, huge bragging rights.**

Mine RTC while playing retro games on your Raspberry Pi. A lightweight edge node for the [RustChain](https://rustchain.org) network that combines hardware attestation mining with [RetroAchievements](https://retroachievements.org) reward integration. Earn RTC tokens for unlocking achievements in classic games -- with rarity-weighted scoring, proof-of-play session boosts, and soulbound cartridge relics for mastered games.

## Quick Install

```bash
git clone https://github.com/Scottcjn/rustchain-arcade.git
cd rustchain-arcade
sudo ./install.sh
```

The installer will detect your hardware, check for RetroArch/RetroPie, find connected controllers, and prompt for your RTC wallet ID and (optionally) RetroAchievements credentials.

## How It Works

- **Mining**: Your Pi submits hardware attestation to the RustChain network every 10 minutes. ARM devices earn 0.0005x weight -- small but honest. Real hardware, real attestation.
- **Proof of Play**: When RetroArch is running, a session tracker monitors your play time and generates heartbeats every 60 seconds. Longer sessions with achievements earn boosted mining weight.
- **Achievements**: The bridge polls RetroAchievements.org for your recently unlocked achievements and converts them into RTC reward claims, weighted by unlock rarity.
- **Cartridge Relics**: Each mastered game becomes a soulbound digital collectible stored locally with ASCII art and stats. Not tradeable, just pure bragging rights.
- **Community Events**: Weekly Saturday Morning Quests, Cabinet Hunts, Arcade Seasons, and the One-Credit Club.
- **Anti-cheat**: Hardware fingerprint checks (clock drift, thermal analysis, VM detection), hardcore-only mode, achievement velocity limits, and tier throttling.

## Achievement Rewards

| Tier | Points | Base RTC | Rarity Multiplier Range |
|------|--------|----------|------------------------|
| Common | 1-5 | 0.00005 | 1.0x - 3.0x |
| Uncommon | 5-10 | 0.0002 | 1.0x - 3.0x |
| Rare | 10-25 | 0.0005 | 1.0x - 3.0x |
| Ultra Rare | 25-50 | 0.001 | 1.0x - 3.0x |
| Legendary | 50-100 | 0.005 | 1.0x - 3.0x |

Achievement value is multiplied by the **rarity factor** based on what percentage of players have unlocked it:

| Unlock Rate | Rarity | Multiplier |
|-------------|--------|------------|
| >50% | Common | 1.0x |
| 20-50% | Uncommon | 1.25x |
| 5-20% | Rare | 1.75x |
| 1-5% | Ultra Rare | 2.5x |
| <1% | Legendary | 3.0x |

Hardcore mode achievements earn **2x** multiplier on top of rarity. Daily wallet cap: 0.10 RTC.

## Proof of Play Boost

Active gaming sessions boost your mining attestation weight. The longer you play (with real achievements), the higher the boost:

| Session Duration | Condition | Boost |
|------------------|-----------|-------|
| 15 minutes | Just playing | 1.5x |
| 30 minutes | + at least 1 achievement | 2.0x |
| 60 minutes | Sustained play | 3.0x |
| Mastery unlocked | Victory Lap | 5.0x |

The base ARM mining weight is 0.0005x. During a Victory Lap, this effectively becomes 0.0025x for the next attestation epoch. The Proof of Play daemon generates a signed heartbeat every 60 seconds as proof that you are actually playing.

## Mastery Milestones

Mastering a game (100% achievements) earns bonus RTC on top of individual achievement rewards:

| Milestone | Bonus RTC | Condition |
|-----------|-----------|-----------|
| First Clear | 0.002 | First achievement in a new game |
| Full Mastery | 0.02 | 100% achievements (softcore) |
| Legendary Mastery | 0.05 | 100% achievements in HARDCORE |
| System Crown | 0.03 | 5 masteries on one platform |

## Cartridge Wallet

Every mastered game is immortalized as a **cartridge relic** -- a soulbound digital collectible stored in `~/.rustchain-arcade/cartridges/`. Each relic includes an ASCII art label:

```
    ╔════════════════════════════════╗
    ║  ┌────────────────────────┐   ║
    ║  │ Super Metroid                │   ║
    ║  │                          │   ║
    ║  │  Platform: SNES           │   ║
    ║  │  Mode:     HARDCORE       │   ║
    ║  │  Cheevos:  64             │   ║
    ║  │  RTC:      0.08500        │   ║
    ║  │                          │   ║
    ║  │  [MASTERED]               │   ║
    ║  └────────────────────────┘   ║
    ║                                ║
    ║  Mastered: 2026-03-21 14:30 UTC ║
    ║   *FIRST PRESS*                ║
    ╚════════════════════════════════╝
```

View your collection:
```bash
python3 /opt/rustchain-arcade/cartridge_wallet.py --list
python3 /opt/rustchain-arcade/cartridge_wallet.py --export > my_profile.json
python3 /opt/rustchain-arcade/cartridge_wallet.py --crowns
```

**First Press**: If you are the first person on the entire RustChain network to master a game, your cartridge gets a special "FIRST PRESS" plaque. Like finding the gold cartridge.

**System Crown**: Master 5 games on one platform (e.g., 5 SNES games) to earn a System Crown badge and 0.03 RTC bonus. Prove your dedication to a platform.

## Community Events

### Saturday Morning Quests

Every week features a different retro platform. Play games on the featured platform to earn a 1.05x bonus on all achievements. The platform rotates through 24 classic systems.

```bash
python3 /opt/rustchain-arcade/community_events.py --featured
```

### Cabinet Hunts

Community-wide goals that rotate weekly. Examples:
- "Clear 50 boss fights across Genesis games this weekend"
- "Beat 25 NES games to completion"
- "Earn 100 SNES achievements"

### Arcade Seasons

Quarterly rankings by unique masteries and platform variety. The more different platforms you master games on, the higher your variety score. Seasons reset every quarter.

### One-Credit Club

Master a game in a single unbroken session -- no saves, no quits, one sitting. The ultimate prestige badge for speedrunners and hardcore players.

## N64 Legend of Elya

[Legend of Elya](https://github.com/Scottcjn/legend-of-elya-n64) is the world's first LLM-powered N64 game -- a homebrew RPG with AI NPCs that generate dialog in real-time on the N64's VR4300 MIPS CPU. The N64 achievement bridge monitors the game's RDRAM state via RetroArch's Network Command Interface and awards RTC for in-game accomplishments.

### N64 Achievements

| Achievement | RTC | How to unlock |
|-------------|-----|---------------|
| First Contact | 1.0 | Talk to Sophia Elya for the first time |
| Scholar's Path | 0.5 | Visit the Arcane Library |
| Forge Born | 0.5 | Visit the Ember Forge |
| Explorer | 2.0 | Visit all 3 rooms in one session |
| Custom Prompt | 1.5 | Use the virtual keyboard to type a custom prompt |
| Polyglot | 3.0 | Talk to all 3 NPCs in one session |
| Deep Thinker | 1.0 | Generate 100+ tokens total |
| Philosopher | 2.0 | Generate 500+ tokens total |
| The Sage's Apprentice | 5.0 | Complete 10 dialogs with Sophia |
| Master of Elya | 10.0 | All other achievements unlocked (soulbound relic) |

**Total possible: 26.5 RTC** (before multipliers).

### N64 Antiquity Multiplier

| Mode | Multiplier | Total RTC |
|------|-----------|-----------|
| Emulated (RetroArch on RPi/PC) | 1.5x | 39.75 |
| Real N64 (EverDrive + bridge) | 3.0x | 79.5 |

Session boost multipliers stack on top of antiquity.

### Running the N64 Bridge

```bash
# Start RetroArch with network commands enabled
retroarch -L mupen64plus_next_libretro.so legend_of_elya.z64 --cmd-port 55355

# In another terminal, start the bridge
python3 n64_elya_bridge.py

# Real N64 hardware mode (3.0x antiquity)
python3 n64_elya_bridge.py --real-n64

# Preview without submitting RTC
python3 n64_elya_bridge.py --dry-run

# Specify GameCtx memory address manually
python3 n64_elya_bridge.py --addr 0x100000

# List all achievements
python3 n64_elya_bridge.py --list-achievements

# Check progress
python3 n64_elya_bridge.py --status
```

The bridge auto-detects the GameCtx struct in RDRAM by scanning for valid state patterns. If auto-detection fails, find the address with `nm legend_of_elya.elf | grep ' G$'` and pass it via `--addr`.

### How It Works

1. RetroArch loads Legend of Elya ROM with an N64 core (mupen64plus-next or parallel-n64)
2. The bridge connects to RetroArch's UDP command interface on port 55355
3. Every 250ms, it reads the GameCtx struct from N64 RDRAM using `READ_CORE_RAM`
4. It tracks room visits, NPC conversations, keyboard usage, and token generation
5. When an achievement condition is met, it submits an RTC reward claim to the RustChain node
6. The Proof of Play daemon tracks session duration for boost multipliers
7. "Master of Elya" mints a soulbound Cartridge Relic with N64/LLM/HOMEBREW badges

### RetroArch Network Commands Setup

Enable network commands in RetroArch:
- **Settings > Network > Network Commands**: On
- **Settings > Network > Network Command Port**: 55355

Or add to `retroarch.cfg`:
```
network_cmd_enable = "true"
network_cmd_port = "55355"
```

## Anti-Cheat

- **Hardcore only**: By default, only RetroAchievements hardcore mode achievements earn RTC (no save states, no cheats)
- **Velocity limit**: More than 20 achievements per hour triggers a pause (flags suspicious activity)
- **Tier throttling**: After 8 common/uncommon achievements in one game per day, those tiers pay half
- **Hardware fingerprint**: Clock drift, thermal analysis, and VM detection ensure real Raspberry Pi hardware
- **Session heartbeats**: Proof of Play generates signed heartbeats every 60 seconds to verify actual gaming sessions

## Configuration

Edit `/opt/rustchain-arcade/config.json` or use environment variables:

| Variable | Purpose |
|----------|---------|
| `SOPHIA_WALLET` | RTC wallet ID |
| `SOPHIA_NODE_URL` | RustChain node URL |
| `RA_USERNAME` | RetroAchievements username |
| `RA_API_KEY` | RetroAchievements API key |

## Manage Services

```bash
# Core miner
sudo systemctl status sophia-miner
sudo journalctl -u sophia-miner -f

# Achievement bridge
sudo systemctl status sophia-achievements
sudo journalctl -u sophia-achievements -f

# Proof of Play session tracker
sudo systemctl status sophia-proof-of-play
sudo journalctl -u sophia-proof-of-play -f
```

## Supported Hardware

- Raspberry Pi 5 (BCM2712)
- Raspberry Pi 4 (BCM2711)
- Other ARM SBCs (aarch64/armv7l) -- should work, not guaranteed

## Requirements

- Raspberry Pi OS (64-bit recommended) or any Debian-based ARM Linux
- Python 3.9+
- Network access to RustChain node
- [RetroAchievements](https://retroachievements.org) account (free, for achievement rewards)
- [RetroArch](https://www.retroarch.com/) (for Proof of Play session tracking)
- A controller and a CRT are optional but earn authenticity bonus

## Links

- [RustChain](https://rustchain.org) -- The network
- [BoTTube](https://bottube.ai) -- AI video platform
- [RetroAchievements](https://retroachievements.org) -- Track your retro gaming progress
- [RetroArch](https://www.retroarch.com/) -- Multi-system emulator frontend
- [Elyan Labs](https://github.com/Scottcjn) -- Who built this

## License

MIT
