# Supported Game Systems & Achievement Platforms

## RetroAchievements.org Integration

rustchain-arcade connects to [RetroAchievements.org](https://retroachievements.org) — a community-driven achievement platform for retro games. RA supports 40+ systems with over 400,000 achievements across 30,000+ games.

### Supported Platforms

| System | RA ID | Core | Era | Notable Games |
|--------|-------|------|-----|---------------|
| NES | 7 | FCEUmm | 1983 | Super Mario Bros, Zelda, Mega Man |
| SNES | 3 | Snes9x | 1990 | Chrono Trigger, Super Metroid, Link to the Past |
| Genesis/Mega Drive | 1 | Genesis Plus GX | 1988 | Sonic, Streets of Rage, Phantasy Star |
| Game Boy | 4 | Gambatte | 1989 | Pokemon Red/Blue, Link's Awakening |
| Game Boy Advance | 5 | mGBA | 2001 | Pokemon Emerald, Metroid Fusion |
| N64 | 2 | Mupen64Plus | 1996 | Mario 64, Ocarina of Time, GoldenEye |
| PlayStation | 12 | PCSX ReARMed | 1994 | FF7, Castlevania SOTN, MGS |
| Saturn | 39 | Beetle Saturn | 1994 | Nights, Panzer Dragoon Saga |
| Arcade | 27 | FBNeo | Various | Street Fighter II, Metal Slug |
| Master System | 11 | Genesis Plus GX | 1985 | Alex Kidd, Phantasy Star |
| Atari 2600 | 25 | Stella | 1977 | Pitfall, Adventure |
| PC Engine | 8 | Beetle PCE | 1987 | Bonk, Castlevania Rondo |
| Neo Geo | 24 | FBNeo | 1990 | KOF, Metal Slug |
| Dreamcast | 40 | Flycast | 1998 | Shenmue, Jet Set Radio |
| DS | 18 | melonDS | 2004 | Pokemon HeartGold, NSMB |

### How Achievements Work

1. Install RetroArch on your RPi (comes with RetroPie)
2. Create a free account at retroachievements.org
3. Enable achievements in RetroArch settings
4. Play games — achievements unlock automatically
5. rustchain-arcade polls RA API for your unlocks
6. RTC rewards are calculated and submitted

### Hardcore Mode

RetroAchievements has two modes:
- **Softcore**: Save states, rewind, cheats allowed. Standard rewards.
- **Hardcore**: No save states, no rewind, no cheats. **2x RTC multiplier.**

rustchain-arcade only counts **Hardcore** achievements by default. This prevents save-scumming for rewards.

## Reward Economics

### Achievement Score Formula

```
achievement_score = ra_points × rarity_factor
```

**Rarity Factor** (based on RA unlock percentage):
| Unlock Rate | Factor | Meaning |
|-------------|--------|---------|
| > 50% | 1.0x | Most players get this |
| 20-50% | 1.25x | Takes some effort |
| 5-20% | 1.75x | Real skill required |
| 1-5% | 2.5x | Dedicated players only |
| < 1% | 3.0x | Legendary difficulty |

### Reward Tiers

| Tier | Score Range | RTC | Hardcore RTC |
|------|------------|-----|-------------|
| Common | 1-5 | 0.0001 | 0.0002 |
| Uncommon | 6-15 | 0.00025 | 0.0005 |
| Rare | 16-40 | 0.00075 | 0.0015 |
| Epic | 41-90 | 0.0025 | 0.005 |
| Legendary | 91+ | 0.0075 | 0.015 |

*Single achievement cap: 0.01 RTC*

### Mastery Milestones

| Milestone | RTC | Collectible |
|-----------|-----|-------------|
| First Clear (beat the game) | 0.002 | Ticket Stub badge |
| Full Mastery (100% achievements) | 0.02 | Soulbound Cartridge Relic |
| Legendary Mastery (100% hardcore) | 0.05 | Animated gold-frame relic |
| System Crown (5 masteries, one platform) | 0.03 | System Crown badge |
| First Press (first mastery of a game on RustChain) | cosmetic | First Press plaque |

### Anti-Inflation Guardrails

- **Daily wallet cap**: 0.10 RTC per wallet per day
- **Network daily cap**: 3.0 RTC total across all gaming nodes
- **Pro-rata**: If claims exceed budget, rewards scale proportionally
- **Tier throttle**: After 8 Common/Uncommon in one game per day, those tiers pay half
- **Velocity check**: More than 20 achievements per hour = flagged for review

## Proof of Play

### Session Boost Multipliers

Active gaming sessions boost your mining attestation weight:

| Session | Boost | Effective ARM Weight |
|---------|-------|---------------------|
| Idle (no game) | 1.0x | 0.0005x |
| 15 min verified play | 1.5x | 0.00075x |
| 30 min + 1 achievement | 2.0x | 0.001x |
| 60+ min stable session | 3.0x | 0.0015x |
| Mastery Victory Lap (next epoch) | 5.0x | 0.0025x |

### How It Works

1. `proof_of_play.py` detects RetroArch running
2. Sends heartbeats every 60 seconds (ROM hash, controller status, CPU temp)
3. Calculates session duration and boost
4. Mining attestation includes proof_of_play data
5. Server applies boost to mining weight for that epoch

### Anti-Cheat

- **Heartbeats must be consistent** — gaps > 5 min reset the session
- **ROM hash must match** — can't swap games and claim continuous session
- **Controller must be connected** — no "let it run" AFK farming
- **Temperature must fluctuate naturally** — bots show flat thermal profiles
- **Achievement velocity** — too fast = flagged

## Community Events

### Saturday Morning Quests
Every Saturday, a featured platform gets a 1.05x bonus on all achievement rewards. Community votes on next week's platform via Discord.

### Cabinet Hunts
Community-wide goals: "Collectively defeat 50 Genesis bosses this weekend." Everyone who contributes gets a commemorative badge.

### Arcade Seasons
Quarterly rankings by:
- Unique game masteries (not raw achievement count)
- Platform variety (playing across different systems scores higher)
- Hardcore completion rate

### One-Credit Club
Prestige-only recognition for single-session clean clears. Almost no extra RTC — pure bragging rights. The hardest flex in retro gaming.

## The Cartridge Wallet

Every mastered game becomes a **Cartridge Relic** in your wallet — a soulbound collectible that can't be traded.

```
┌──────────────────────────────────┐
│  ╔══════════════════════════╗    │
│  ║   SUPER METROID          ║    │
│  ║   ━━━━━━━━━━━━━━━━━━━━   ║    │
│  ║   Platform: SNES         ║    │
│  ║   Mastered: 2026-03-21   ║    │
│  ║   Achievements: 44/44    ║    │
│  ║   Mode: HARDCORE         ║    │
│  ║   RTC Earned: 0.087      ║    │
│  ║   ━━━━━━━━━━━━━━━━━━━━   ║    │
│  ║   🏆 CARTRIDGE RELIC     ║    │
│  ╚══════════════════════════╝    │
│            ▓▓▓▓▓▓                │
└──────────────────────────────────┘
```

Your **Memory Card** profile shows all your cartridges, favorite system, total masteries, authenticity badges, and setup story. Share it, flex it, earn it.

## Motto

> **Small RTC, huge bragging rights.**
>
> The arcade ticket is the RTC. The trophy is the relic.
> RustChain honors real retro play on real hardware.
