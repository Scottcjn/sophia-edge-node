# Contributing to RustChain Arcade

Thank you for your interest in contributing to RustChain Arcade! This guide will help you get started with the project and understand how to contribute effectively.

## About the Project

RustChain Arcade is a retro gaming + blockchain mining hybrid for Raspberry Pi. Players earn RTC tokens by unlocking achievements in classic games through RetroAchievements integration, with rarity-weighted scoring, proof-of-play session boosts, and soulbound cartridge relics.

## Getting Started

### Prerequisites

- Raspberry Pi 4 or 5 (or compatible ARM SBC)
- Raspberry Pi OS (64-bit recommended) or Debian-based ARM Linux
- Python 3.9+
- Git
- (Optional) RetroArch for Proof of Play testing
- (Optional) RetroAchievements account for achievement integration testing

### Development Setup

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/rustchain-arcade.git
   cd rustchain-arcade
   ```
3. **Create a virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
4. **Install dependencies** (check individual Python files for imports)
5. **Review the codebase**:
   - `achievement_bridge.py` - RetroAchievements integration
   - `proof_of_play.py` - Session tracking and heartbeats
   - `cartridge_wallet.py` - Soulbound relic management
   - `rustchain_miner.py` - Core mining logic
   - `install.sh` - Installation script

## Development Workflow

1. **Create a branch** for your changes:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following the code style guidelines below

3. **Test your changes** on actual Raspberry Pi hardware when possible

4. **Commit your changes**:
   ```bash
   git commit -m "feat: description of your changes"
   ```

5. **Push to your fork**:
   ```bash
   git push origin feature/your-feature-name
   ```

6. **Open a Pull Request** on GitHub

## Code Style

- **Python 3.9+** syntax
- Follow **PEP 8** style guidelines
- Use **type hints** where appropriate
- Add **docstrings** for functions and classes
- Keep functions focused and modular
- Comment complex logic explaining the "why"

### Example:
```python
def calculate_rarity_multiplier(unlock_rate: float) -> float:
    """
    Calculate rarity multiplier based on achievement unlock rate.
    
    Args:
        unlock_rate: Percentage of players who have unlocked (0-100)
    
    Returns:
        Multiplier value (1.0x to 3.0x)
    """
    if unlock_rate > 50:
        return 1.0
    elif unlock_rate > 20:
        return 1.25
    elif unlock_rate > 5:
        return 1.75
    elif unlock_rate > 1:
        return 2.5
    else:
        return 3.0
```

## Testing

Since RustChain Arcade runs on Raspberry Pi hardware:

- Test on actual Pi hardware when possible
- Verify RetroArch integration if modifying achievement bridge
- Check systemd service files if modifying startup logic
- Test installer script on fresh Pi OS installs

## Areas for Contribution

### High Priority
- New game platform support
- Improved anti-cheat mechanisms
- Performance optimizations for Pi Zero 2W
- Better error handling and logging

### Documentation
- Tutorial videos or written guides
- Translation of README to other languages
- Troubleshooting guides
- Hardware compatibility lists

### Features
- New community event types
- Enhanced cartridge relic visualization
- Discord bot integration
- Web dashboard for stats

## Pull Request Guidelines

1. **Describe what changed and why** in your PR description
2. **Link related issues** if applicable
3. **Include testing notes** - how did you verify this works?
4. **Keep changes focused** - one feature/fix per PR
5. **Update documentation** if your change affects user-facing behavior

## Reporting Issues

### Bugs
- Describe the bug clearly
- Include steps to reproduce
- Provide hardware info (Pi model, OS version)
- Include relevant log output: `sudo journalctl -u sophia-miner -n 50`

### Feature Requests
- Explain the use case
- Describe the desired behavior
- Consider implementation complexity

## Security

If you discover a security vulnerability:
- **DO NOT** open a public issue
- Contact the maintainers privately
- Include reproduction steps
- Allow time for a fix before disclosure

## Bounty Program

Check out [rustchain-bounties](https://github.com/Scottcjn/rustchain-bounties) for RTC-paid tasks related to RustChain Arcade and other Elyan Labs projects!

## Code of Conduct

- Be respectful and constructive
- Welcome newcomers
- Focus on the code, not the person
- Assume good intentions

## Questions?

- Open an issue for questions
- Join the RustChain Discord: https://discord.gg/cafc4nDV
- Check existing documentation first

## License

By contributing, you agree that your contributions will be licensed under the same MIT license as the project.

---

**Happy gaming and mining!** 🎮⛏️
