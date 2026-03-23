#!/usr/bin/env bash
# rustchain-arcade installer for Raspberry Pi 4 / 5
# Creates /opt/rustchain-arcade/, installs dependencies, sets up systemd services.
set -euo pipefail

INSTALL_DIR="/opt/rustchain-arcade"
STATE_DIR="$HOME/.rustchain-arcade"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { echo -e "\033[1;32m[+]\033[0m $*"; }
warn()  { echo -e "\033[1;33m[!]\033[0m $*"; }
error() { echo -e "\033[1;31m[-]\033[0m $*"; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        error "Run this script as root (sudo ./install.sh)"
    fi
}

# ---------------------------------------------------------------------------
# Hardware detection
# ---------------------------------------------------------------------------

detect_hardware() {
    local hw
    hw=$(grep -i 'Hardware' /proc/cpuinfo 2>/dev/null | head -1 | cut -d: -f2 | tr -d ' ' || true)
    case "$hw" in
        *BCM2712*) info "Detected Raspberry Pi 5 (BCM2712)"; return 0 ;;
        *BCM2711*) info "Detected Raspberry Pi 4 (BCM2711)"; return 0 ;;
        *BCM*)     info "Detected Broadcom SoC ($hw) -- should work"; return 0 ;;
    esac

    local arch
    arch=$(uname -m)
    if [[ "$arch" == "aarch64" || "$arch" == "armv7l" ]]; then
        warn "Not a Raspberry Pi but ARM detected ($arch). Proceeding anyway."
        return 0
    fi

    error "Unsupported architecture: $arch. This installer targets Raspberry Pi 4/5."
}

# ---------------------------------------------------------------------------
# RetroArch / RetroPie detection
# ---------------------------------------------------------------------------

detect_retroarch() {
    info "Checking for RetroArch / RetroPie..."

    local found_retroarch=false

    # Check for RetroArch binary
    if command -v retroarch &>/dev/null; then
        local ra_version
        ra_version=$(retroarch --version 2>&1 | head -1 || echo "unknown version")
        info "RetroArch found: $ra_version"
        found_retroarch=true
    fi

    # Check for RetroPie installation
    if [[ -d "/opt/retropie" ]]; then
        info "RetroPie installation detected at /opt/retropie"
        found_retroarch=true
    fi

    # Check for flatpak RetroArch
    if flatpak list 2>/dev/null | grep -qi retroarch; then
        info "RetroArch (Flatpak) detected"
        found_retroarch=true
    fi

    # Check for snap RetroArch
    if snap list 2>/dev/null | grep -qi retroarch; then
        info "RetroArch (Snap) detected"
        found_retroarch=true
    fi

    if [[ "$found_retroarch" == "false" ]]; then
        warn "RetroArch not found. Proof of Play features require RetroArch."
        warn "Install: sudo apt install retroarch  (or set up RetroPie)"
        warn "Mining will still work without it."
    fi
}

# ---------------------------------------------------------------------------
# Controller detection
# ---------------------------------------------------------------------------

detect_controllers() {
    info "Checking for connected controllers..."
    local count=0

    # Check /dev/input/js* joystick devices
    for js in /dev/input/js*; do
        if [[ -e "$js" ]]; then
            local js_num="${js##/dev/input/js}"
            local name_path="/sys/class/input/js${js_num}/device/name"
            if [[ -r "$name_path" ]]; then
                local name
                name=$(cat "$name_path")
                info "  Controller $count: $name ($js)"
            else
                info "  Controller $count: Unknown ($js)"
            fi
            ((count++))
        fi
    done

    # Also check lsusb for known controller USB vendor IDs
    if command -v lsusb &>/dev/null; then
        local known_ids=(
            "054c"  # Sony (DualShock, DualSense)
            "045e"  # Microsoft (Xbox controllers)
            "057e"  # Nintendo (Switch Pro, Joy-Con)
            "28de"  # Valve (Steam Controller)
            "2dc8"  # 8BitDo
            "0079"  # DragonRise (cheap USB gamepads)
            "0583"  # Padix (retro USB adapters)
            "0810"  # Personal Communication Systems (retro adapters)
        )
        for vid in "${known_ids[@]}"; do
            local match
            match=$(lsusb 2>/dev/null | grep -i "ID ${vid}:" || true)
            if [[ -n "$match" ]]; then
                info "  USB controller detected: $match"
            fi
        done
    fi

    if [[ $count -eq 0 ]]; then
        warn "No joystick devices found. Controllers optional but recommended."
    else
        info "$count controller(s) detected"
    fi
}

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

install_deps() {
    info "Installing system packages..."
    apt-get update -qq
    apt-get install -y -qq python3 python3-pip python3-venv > /dev/null

    # SDL2 dev libs for HUD overlay (optional, fallback to text if unavailable)
    apt-get install -y -qq libsdl2-2.0-0 > /dev/null 2>&1 || \
        warn "SDL2 not available -- HUD overlay will use text fallback"

    # Audio tools for notification sounds (optional)
    apt-get install -y -qq alsa-utils > /dev/null 2>&1 || \
        warn "alsa-utils not available -- sound notifications disabled"

    # Check Python version (need 3.9+)
    local pyver
    pyver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local major minor
    major=$(echo "$pyver" | cut -d. -f1)
    minor=$(echo "$pyver" | cut -d. -f2)
    if (( major < 3 || (major == 3 && minor < 9) )); then
        error "Python 3.9+ required (found $pyver)"
    fi
    info "Python $pyver OK"
}

setup_venv() {
    info "Setting up Python virtual environment..."
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install --quiet aiohttp requests
    info "Python dependencies installed"
}

# ---------------------------------------------------------------------------
# File installation
# ---------------------------------------------------------------------------

install_files() {
    info "Installing rustchain-arcade to $INSTALL_DIR ..."
    mkdir -p "$INSTALL_DIR"

    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

    cp "$script_dir/rustchain_miner.py" "$INSTALL_DIR/"
    cp "$script_dir/achievement_bridge.py" "$INSTALL_DIR/"
    cp "$script_dir/proof_of_play.py" "$INSTALL_DIR/"
    cp "$script_dir/cartridge_wallet.py" "$INSTALL_DIR/"
    cp "$script_dir/community_events.py" "$INSTALL_DIR/"
    cp "$script_dir/hud_overlay.py" "$INSTALL_DIR/"
    cp "$script_dir/leaderboard.py" "$INSTALL_DIR/"
    cp "$script_dir/controller_detect.py" "$INSTALL_DIR/"
    cp "$script_dir/daily_digest.py" "$INSTALL_DIR/"
    cp "$script_dir/game_recommender.py" "$INSTALL_DIR/"
    cp "$script_dir/config.json" "$INSTALL_DIR/"

    chmod +x "$INSTALL_DIR/rustchain_miner.py"
    chmod +x "$INSTALL_DIR/achievement_bridge.py"
    chmod +x "$INSTALL_DIR/proof_of_play.py"
    chmod +x "$INSTALL_DIR/cartridge_wallet.py"
    chmod +x "$INSTALL_DIR/community_events.py"
    chmod +x "$INSTALL_DIR/hud_overlay.py"
    chmod +x "$INSTALL_DIR/leaderboard.py"
    chmod +x "$INSTALL_DIR/controller_detect.py"
    chmod +x "$INSTALL_DIR/daily_digest.py"
    chmod +x "$INSTALL_DIR/game_recommender.py"

    # Copy sounds directory
    mkdir -p "$INSTALL_DIR/sounds"
    if [[ -d "$script_dir/sounds" ]]; then
        cp -r "$script_dir/sounds/"* "$INSTALL_DIR/sounds/" 2>/dev/null || true
        info "Sounds directory installed"
    fi

    # State directory structure
    mkdir -p "$STATE_DIR"
    mkdir -p "$STATE_DIR/sessions"
    mkdir -p "$STATE_DIR/cartridges"
    mkdir -p "$STATE_DIR/events"
    mkdir -p "$STATE_DIR/digests"

    info "Files installed"
}

# ---------------------------------------------------------------------------
# Configuration prompts
# ---------------------------------------------------------------------------

configure() {
    local config="$INSTALL_DIR/config.json"
    local wallet ra_user ra_key node_id

    echo ""
    info "=== Configuration ==="
    echo ""

    # Node ID
    local default_node_id="rustchain-arcade-$(hostname -s)"
    read -rp "Node ID [$default_node_id]: " node_id
    node_id="${node_id:-$default_node_id}"

    # Wallet
    read -rp "RTC Wallet ID (leave blank to use node ID): " wallet
    wallet="${wallet:-$node_id}"

    # RetroAchievements
    echo ""
    info "RetroAchievements integration (optional)"
    info "Sign up at https://retroachievements.org if you don't have an account."
    echo ""
    read -rp "RetroAchievements username (blank to skip): " ra_user
    ra_key=""
    if [[ -n "$ra_user" ]]; then
        read -rp "RetroAchievements API key: " ra_key
    fi

    # Write config using python to avoid jq dependency
    python3 - "$config" "$node_id" "$ra_user" "$ra_key" <<'PYEOF'
import json, sys
config_path, node_id, ra_user, ra_key = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(config_path) as f:
    cfg = json.load(f)
cfg["node_id"] = node_id
cfg["achievements"]["retroachievements"]["username"] = ra_user
cfg["achievements"]["retroachievements"]["api_key"] = ra_key
if not ra_user:
    cfg["achievements"]["enabled"] = False
with open(config_path, "w") as f:
    json.dump(cfg, f, indent=2)
PYEOF

    # Write wallet to environment file for systemd
    cat > "$INSTALL_DIR/env" <<EOF
SOPHIA_WALLET=$wallet
SOPHIA_CONFIG=$INSTALL_DIR/config.json
RA_USERNAME=$ra_user
RA_API_KEY=$ra_key
EOF
    chmod 600 "$INSTALL_DIR/env"

    info "Configuration saved"
}

# ---------------------------------------------------------------------------
# Systemd services
# ---------------------------------------------------------------------------

install_services() {
    info "Installing systemd services..."

    # Miner service
    cat > /etc/systemd/system/sophia-miner.service <<EOF
[Unit]
Description=RustChain Arcade - RustChain Miner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$INSTALL_DIR/env
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/rustchain_miner.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # Achievement bridge service
    cat > /etc/systemd/system/sophia-achievements.service <<EOF
[Unit]
Description=RustChain Arcade - RetroAchievements Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$INSTALL_DIR/env
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/achievement_bridge.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # Proof of Play session tracker service
    cat > /etc/systemd/system/sophia-proof-of-play.service <<EOF
[Unit]
Description=RustChain Arcade - Proof of Play Session Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$INSTALL_DIR/env
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/proof_of_play.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # HUD overlay service
    cat > /etc/systemd/system/sophia-hud.service <<EOF
[Unit]
Description=RustChain Arcade - Achievement HUD Overlay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$INSTALL_DIR/env
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/hud_overlay.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # Daily digest timer and service
    cat > /etc/systemd/system/sophia-digest.service <<EOF
[Unit]
Description=RustChain Arcade - Daily Gaming Digest
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$INSTALL_DIR/env
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/daily_digest.py --quiet --save-card --post-discord
StandardOutput=journal
StandardError=journal
EOF

    cat > /etc/systemd/system/sophia-digest.timer <<EOF
[Unit]
Description=RustChain Arcade - Daily Digest Timer (midnight UTC)

[Timer]
OnCalendar=*-*-* 00:05:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

    systemctl daemon-reload
    systemctl enable sophia-miner.service

    # Only enable achievements + proof-of-play + HUD if RetroAchievements configured
    if python3 -c "import json; c=json.load(open('$INSTALL_DIR/config.json')); exit(0 if c.get('achievements',{}).get('enabled') else 1)" 2>/dev/null; then
        systemctl enable sophia-achievements.service
        systemctl enable sophia-proof-of-play.service
        systemctl enable sophia-hud.service
        info "Achievement bridge enabled"
        info "Proof of Play tracker enabled"
        info "HUD overlay enabled"
    else
        info "Achievement bridge disabled (no RetroAchievements credentials)"
        info "Proof of Play tracker disabled (requires RetroAchievements)"
        info "HUD overlay disabled (requires RetroAchievements)"
    fi

    # Always enable daily digest timer
    systemctl enable sophia-digest.timer
    info "Daily digest timer enabled (midnight UTC)"

    info "Systemd services installed"
}

# ---------------------------------------------------------------------------
# Start services
# ---------------------------------------------------------------------------

start_services() {
    echo ""
    read -rp "Start services now? [Y/n]: " start_now
    start_now="${start_now:-Y}"

    if [[ "$start_now" =~ ^[Yy] ]]; then
        systemctl start sophia-miner.service
        info "Miner started"

        if systemctl is-enabled sophia-achievements.service &>/dev/null; then
            systemctl start sophia-achievements.service
            info "Achievement bridge started"
        fi

        if systemctl is-enabled sophia-proof-of-play.service &>/dev/null; then
            systemctl start sophia-proof-of-play.service
            info "Proof of Play tracker started"
        fi

        if systemctl is-enabled sophia-hud.service &>/dev/null; then
            systemctl start sophia-hud.service
            info "HUD overlay started"
        fi

        systemctl start sophia-digest.timer
        info "Daily digest timer started"
    else
        info "Services installed but not started. Use:"
        info "  sudo systemctl start sophia-miner"
        info "  sudo systemctl start sophia-achievements"
        info "  sudo systemctl start sophia-proof-of-play"
        info "  sudo systemctl start sophia-hud"
        info "  sudo systemctl start sophia-digest.timer"
    fi
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

show_summary() {
    echo ""
    info "=== Installation Complete ==="
    echo ""
    echo "  Install dir:  $INSTALL_DIR"
    echo "  State dir:    $STATE_DIR"
    echo "  Config:       $INSTALL_DIR/config.json"
    echo ""
    echo "  Cartridge wallet: $STATE_DIR/cartridges/"
    echo "  Session history:  $STATE_DIR/sessions/"
    echo "  Event data:       $STATE_DIR/events/"
    echo ""
    echo "  Manage services:"
    echo "    sudo systemctl status sophia-miner"
    echo "    sudo systemctl status sophia-achievements"
    echo "    sudo systemctl status sophia-proof-of-play"
    echo "    sudo systemctl status sophia-hud"
    echo "    sudo systemctl list-timers sophia-digest"
    echo "    sudo journalctl -u sophia-miner -f"
    echo "    sudo journalctl -u sophia-achievements -f"
    echo "    sudo journalctl -u sophia-proof-of-play -f"
    echo ""
    echo "  View your collection:"
    echo "    python3 $INSTALL_DIR/cartridge_wallet.py --list"
    echo "    python3 $INSTALL_DIR/community_events.py --events"
    echo "    python3 $INSTALL_DIR/leaderboard.py --local"
    echo "    python3 $INSTALL_DIR/leaderboard.py --network"
    echo "    python3 $INSTALL_DIR/controller_detect.py"
    echo "    python3 $INSTALL_DIR/daily_digest.py --today"
    echo "    python3 $INSTALL_DIR/game_recommender.py --platform snes"
    echo ""
    echo "  RustChain:    https://rustchain.org"
    echo "  BoTTube:      https://bottube.ai"
    echo ""
    echo "  Small RTC, huge bragging rights."
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    echo ""
    echo "  ╔═══════════════════════════════════════╗"
    echo "  ║  RustChain Arcade Installer           ║"
    echo "  ║  Mine RTC + Earn Retro Game Rewards   ║"
    echo "  ║  Small RTC, huge bragging rights.     ║"
    echo "  ╚═══════════════════════════════════════╝"
    echo ""

    require_root
    detect_hardware
    detect_retroarch
    detect_controllers
    install_deps
    install_files
    setup_venv
    configure
    install_services
    start_services
    show_summary
}

main "$@"
