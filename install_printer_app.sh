#!/bin/bash
#
# Vula! Print Label Printer — Installation Script
# Installs as a systemd user service that auto-starts on login.
#
# Usage (either form works):
#   bash install_printer_app.sh        ← recommended
#   sudo bash install_printer_app.sh   ← also works
#

set -e

DEFAULT_API_BASE_URL="https://shop.novaconv.co.za"

# ──────────────────────────────────────────────────────────────────
# Resolve the REAL user and their home, regardless of whether the
# script was invoked with or without sudo.
#
#   sudo ./install_printer_app.sh  → EUID=0, SUDO_USER=nero
#   ./install_printer_app.sh       → EUID=1000, SUDO_USER unset
# ──────────────────────────────────────────────────────────────────
if [ "$EUID" -eq 0 ] && [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    REAL_USER="$USER"
    REAL_HOME="$HOME"
fi

# Helper: run a command AS the real (non-root) user.
# Falls through to a plain call when not running as root.
as_user() {
    if [ "$EUID" -eq 0 ]; then
        sudo -u "$REAL_USER" --preserve-env=DISPLAY,WAYLAND_DISPLAY,DBUS_SESSION_BUS_ADDRESS \
            env HOME="$REAL_HOME" XDG_RUNTIME_DIR="/run/user/$(id -u "$REAL_USER")" \
            "$@"
    else
        "$@"
    fi
}

# Helper: run apt-get (needs root).
apt_install() {
    if [ "$EUID" -eq 0 ]; then
        apt-get "$@"
    else
        sudo apt-get "$@"
    fi
}

# Helper: trim leading/trailing whitespace.
trim() {
    local s="$1"
    # shellcheck disable=SC2001
    s="$(echo "$s" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    printf '%s' "$s"
}

# Prompt for backend settings and write .env (or use env vars for non-interactive runs).
configure_vula_env() {
    local env_file="$SCRIPT_DIR/.env"
    local existing_user_id=""
    local input_url=""
    local input_key=""

    if [ -f "$env_file" ]; then
        existing_user_id="$(grep -E '^PRINTER_USER_ID=' "$env_file" | tail -n1 | cut -d'=' -f2-)"
    fi
    existing_user_id="$(trim "$existing_user_id")"
    [ -n "$existing_user_id" ] || existing_user_id="1"

    if [ -n "$PRINTER_API_BASE_URL" ] || [ -n "$PRINTER_API_KEY" ]; then
        input_url="$(trim "${PRINTER_API_BASE_URL:-$DEFAULT_API_BASE_URL}")"
        input_key="$(trim "${PRINTER_API_KEY:-}")"
        if [ -z "$input_key" ]; then
            echo "❌ PRINTER_API_KEY env var was provided but is empty."
            echo "   Set PRINTER_API_KEY and re-run installer."
            exit 1
        fi
    elif [ -t 0 ]; then
        echo ""
        echo "🔑 Vula API Setup"
        echo "   Configure backend access before starting the service."

        while true; do
            read -r -p "   API base URL [$DEFAULT_API_BASE_URL]: " input_url
            input_url="$(trim "${input_url:-$DEFAULT_API_BASE_URL}")"
            if [ -n "$input_url" ]; then
                break
            fi
            echo "   API base URL is required."
        done

        while true; do
            read -r -s -p "   API key: " input_key
            echo ""
            input_key="$(trim "$input_key")"
            if [ -n "$input_key" ]; then
                break
            fi
            echo "   API key is required."
        done
    else
        echo "❌ Non-interactive install detected with no API credentials provided."
        echo "   Re-run interactively or pass env vars:"
        echo "   PRINTER_API_BASE_URL=https://shop.novaconv.co.za PRINTER_API_KEY=... bash install_printer_app.sh"
        exit 1
    fi

    cat > "$env_file" <<EOF
# Vula! Print app environment variables
# Managed by install_printer_app.sh
PRINTER_API_BASE_URL=$input_url
PRINTER_API_KEY=$input_key
PRINTER_USER_ID=$existing_user_id
EOF
    chmod 600 "$env_file"
    echo "   ✓ Saved backend settings to $env_file"
}

# ──────────────────────────────────────────────────────────────────
# Resolve project directory regardless of how the script was called
# ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

SERVICE_NAME="vula-print"
SERVICE_FILE="$REAL_HOME/.config/systemd/user/${SERVICE_NAME}.service"
LEGACY_AUTOSTART="$REAL_HOME/.config/autostart/vula-print.desktop"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Vula! Print Label Printer — Installer              ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Installing for user: $REAL_USER  ($REAL_HOME)"
echo ""

# ── 1. OS check ───────────────────────────────────────────────────
if [ ! -f /etc/debian_version ]; then
    echo "⚠  Warning: This script targets Debian/Ubuntu systems."
    read -p "   Continue anyway? (y/N) " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

# ── 2. System dependencies (always needs root) ────────────────────
echo "📦 Installing system dependencies…"
apt_install update -qq
apt_install install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-pyqt6 \
    libusb-1.0-0 \
    cups \
    git
echo "   ✓ System packages OK"

# ── 3. Printer device permissions ────────────────────────────────
# /dev/usb/lp* devices are owned by root:lp (mode 0660).
# The running user must be in the 'lp' group to open them directly
# without sudo.  We also install a udev rule so the group is applied
# even if the default distro rule is missing.
echo ""
echo "🖨  Setting up printer device permissions…"

# a) Create udev rule (needs root)
UDEV_RULE="/etc/udev/rules.d/60-usb-label-printer.rules"
if [ "$EUID" -eq 0 ]; then
    cat > "$UDEV_RULE" <<'UDEV'
# Grant the 'lp' group read-write access to USB printer devices
SUBSYSTEM=="usb", KERNEL=="lp[0-9]*", GROUP="lp", MODE="0664"
UDEV
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=usb 2>/dev/null || true
    echo "   ✓ udev rule written: $UDEV_RULE"
else
    sudo bash -c "cat > '$UDEV_RULE' <<'UDEV'
# Grant the 'lp' group read-write access to USB printer devices
SUBSYSTEM==\"usb\", KERNEL==\"lp[0-9]*\", GROUP=\"lp\", MODE=\"0664\"
UDEV"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=usb 2>/dev/null || true
    echo "   ✓ udev rule written: $UDEV_RULE"
fi

# b) Add real user to 'lp' group
if id -nG "$REAL_USER" | grep -qw lp; then
    echo "   ✓ $REAL_USER is already in the 'lp' group"
else
    if [ "$EUID" -eq 0 ]; then
        usermod -aG lp "$REAL_USER"
    else
        sudo usermod -aG lp "$REAL_USER"
    fi
    echo "   ✓ Added $REAL_USER to the 'lp' group"
    echo "   ⚠  Group change takes effect on next login / reboot."
    echo "      For this session, run:  newgrp lp"
fi


# ── 4. Python virtual environment (must run as REAL_USER) ────────
echo ""
echo "🐍 Setting up Python virtual environment…"

# Remove any stale venv that was created as the wrong user.
# We do this as root (rm -rf) then recreate as the real user.
if [ -d "$SCRIPT_DIR/venv" ]; then
    VENV_OWNER=$(stat -c '%U' "$SCRIPT_DIR/venv" 2>/dev/null || echo "unknown")
    if [ "$VENV_OWNER" != "$REAL_USER" ]; then
        echo "   ↳ Removing stale venv owned by '$VENV_OWNER'…"
        rm -rf "$SCRIPT_DIR/venv"
        echo "   ✓ Stale venv removed"
    else
        echo "   ↳ Existing venv owned by '$REAL_USER' — will reuse"
    fi
fi

# --system-site-packages lets the venv inherit python3-pyqt6, python3-pip etc.
# that were just installed via apt.  This is MUCH faster than re-downloading
# PyQt6 (~50 MB) through pip and avoids the "frozen" appearance.
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "   ↳ Creating venv (this takes ~10 seconds)…"
    as_user python3 -m venv --system-site-packages "$SCRIPT_DIR/venv"
    echo "   ✓ venv created"
fi

# Only install the packages that are NOT already provided by the system.
# pip will skip anything already present — output is visible (no -q flag).
echo "   ↳ Installing/verifying Python packages…"
echo "   ℹ  PyQt6 comes from the system apt package — no large download needed."
as_user "$SCRIPT_DIR/venv/bin/pip" install -r "$SCRIPT_DIR/requirements_app.txt"
echo "   ✓ venv ready  (owner: $REAL_USER)"

# ── 5. Make scripts executable ───────────────────────────────────
chmod +x "$SCRIPT_DIR/vula_print_app.py"
chmod +x "$SCRIPT_DIR/launch_printer.sh"
[ -f "$SCRIPT_DIR/update.sh" ]    && chmod +x "$SCRIPT_DIR/update.sh"
[ -f "$SCRIPT_DIR/uninstall.sh" ] && chmod +x "$SCRIPT_DIR/uninstall.sh"

# ── 5b. Backend credentials are handled by the GUI onboarding wizard.
# The installer must NOT prompt for API credentials.
if [ -n "$PRINTER_API_BASE_URL" ] && [ -n "$PRINTER_API_KEY" ]; then
    printf '%s\n' \
        "PRINTER_API_BASE_URL=$PRINTER_API_BASE_URL" \
        "PRINTER_API_KEY=$PRINTER_API_KEY" \
        "PRINTER_USER_ID=${PRINTER_USER_ID:-1}" > "$SCRIPT_DIR/.env"
    chmod 600 "$SCRIPT_DIR/.env"
    echo "   ✓ Wrote .env from provided environment variables only."
else
    rm -f "$SCRIPT_DIR/.env"
    echo "   ℹ  No .env created — first-run GUI setup will configure the app."
fi

# Ensure the whole project directory is owned by the real user
if [ "$EUID" -eq 0 ]; then
    chown -R "$REAL_USER":"$REAL_USER" "$SCRIPT_DIR"
fi

# ── 6. Remove legacy desktop-session autostart (replaced by service) ─
if [ -f "$LEGACY_AUTOSTART" ]; then
    rm -f "$LEGACY_AUTOSTART"
    echo "   ↳ Removed legacy autostart desktop entry"
fi

# ── 7. Write systemd user service ────────────────────────────────
echo ""
echo "⚙  Installing systemd user service…"
as_user mkdir -p "$REAL_HOME/.config/systemd/user"

# Write the service file as the real user
as_user bash -c "cat > '$SERVICE_FILE'" <<EOF
[Unit]
Description=Vula! Print Label Printer
Documentation=https://github.com/Rosso-OmniForge/Raw-Barcode-Printer
After=graphical-session.target network-online.target
Wants=graphical-session.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
# Brief grace period so graphical-session.target is fully settled before
# Qt tries to open a display connection. Prevents a login-time race that
# would otherwise cause a restart storm.
ExecStartPre=/bin/sleep 2
ExecStart=${SCRIPT_DIR}/launch_printer.sh

# Restart only on abnormal exit. A clean exit (operator stop, or the
# singleton guard politely refusing a second launch) must NOT trigger
# a respawn — that is what was stacking instances.
Restart=on-failure
RestartSec=15
StartLimitIntervalSec=300
StartLimitBurst=3

# Give the app a chance to release its lock and printer devices before
# systemd forgets about it. Without this, a quick restart can leave two
# live PIDs briefly competing for /dev/usb/lp*.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=10

[Install]
WantedBy=graphical-session.target
EOF

echo "   ✓ Service file: $SERVICE_FILE"

# ── 8. Enable lingering so user services start at boot ───────────
echo ""
echo "🔐 Enabling loginctl linger for $REAL_USER…"
loginctl enable-linger "$REAL_USER" 2>/dev/null && echo "   ✓ Linger enabled" || \
    echo "   ⚠  Could not enable linger (may need systemd ≥ 230)"

# ── 9. Reload → enable → (re)start the service ───────────────────
echo ""
echo "🚀 Starting service…"
as_user systemctl --user daemon-reload
as_user systemctl --user enable "${SERVICE_NAME}.service"
as_user systemctl --user stop   "${SERVICE_NAME}.service" 2>/dev/null || true
as_user systemctl --user start  "${SERVICE_NAME}.service"

sleep 1
if as_user systemctl --user is-active --quiet "${SERVICE_NAME}.service"; then
    echo "   ✓ Service is RUNNING"
else
    echo "   ⚠  Service not yet active — normal if display session is not"
    echo "      available yet. It will start automatically on next login."
    echo "      Check: systemctl --user status ${SERVICE_NAME}"
fi

# ── 10. Summary ──────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅ Installation Complete!                           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Service:   ${SERVICE_NAME}.service"
echo "  Source:    ${SCRIPT_DIR}"
echo "  User:      ${REAL_USER}"
echo ""
echo "  Useful commands:"
echo "    Status   →  systemctl --user status  ${SERVICE_NAME}"
echo "    Logs     →  journalctl --user -u ${SERVICE_NAME} -f"
echo "    Stop     →  systemctl --user stop    ${SERVICE_NAME}"
echo "    Restart  →  systemctl --user restart ${SERVICE_NAME}"
echo ""
echo "  To update:   bash update.sh"
echo "  To remove:   bash uninstall.sh"
echo "  Env file:    $SCRIPT_DIR/.env"
echo ""
