#!/bin/bash
#
# Vula! Print Label Printer — Uninstall Script
#
# Usage (either form works):
#   bash uninstall.sh
#   sudo bash uninstall.sh
#

# ── Resolve real user (handles sudo invocation) ───────────────────
if [ "$EUID" -eq 0 ] && [ -n "$SUDO_USER" ]; then
    REAL_USER="$SUDO_USER"
    REAL_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    REAL_USER="$USER"
    REAL_HOME="$HOME"
fi

as_user() {
    if [ "$EUID" -eq 0 ]; then
        sudo -u "$REAL_USER" \
            env HOME="$REAL_HOME" XDG_RUNTIME_DIR="/run/user/$(id -u "$REAL_USER")" \
            "$@"
    else
        "$@"
    fi
}

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

SERVICE_NAME="vula-print"
SERVICE_FILE="$REAL_HOME/.config/systemd/user/${SERVICE_NAME}.service"
CONFIG_DIR="$REAL_HOME/.config/vula_print"
LEGACY_AUTOSTART="$REAL_HOME/.config/autostart/vula-print.desktop"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Vula! Print Label Printer — Uninstaller           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  This will remove:"
echo "    • The systemd user service"
echo "    • The Python virtual environment (venv/)"
echo "    • App config and print history (~/.config/vula_print/)"
echo "    • Any legacy autostart desktop entry"
echo ""
echo "  The source-code directory will NOT be deleted."
echo ""

read -p "Continue with uninstall? (y/N) " -n 1 -r
echo
[[ $REPLY =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }

echo ""

# ── 1. Stop + disable systemd service ────────────────────────────
if as_user systemctl --user is-active --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
    echo "🛑 Stopping service…"
    as_user systemctl --user stop "${SERVICE_NAME}.service"
    echo "   ✓ Stopped"
fi

if as_user systemctl --user is-enabled --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
    echo "   Disabling service…"
    as_user systemctl --user disable "${SERVICE_NAME}.service"
    echo "   ✓ Disabled"
fi

# ── 2. Remove service file ────────────────────────────────────────
if [ -f "$SERVICE_FILE" ]; then
    rm -f "$SERVICE_FILE"
    echo "   ✓ Removed service file: $SERVICE_FILE"
fi

as_user systemctl --user daemon-reload 2>/dev/null || true
as_user systemctl --user reset-failed  2>/dev/null || true
echo "   ✓ systemd state cleared"

# ── 3. Remove Python virtual environment ─────────────────────────
echo ""
echo "🐍 Removing virtual environment…"
if [ -d "$SCRIPT_DIR/venv" ]; then
    rm -rf "$SCRIPT_DIR/venv"
    echo "   ✓ venv/ removed"
else
    echo "   ℹ  venv/ not found — skipping"
fi

# ── 4. Remove app config & history ───────────────────────────────
echo ""
echo "🗂  Removing app config and history…"
if [ -d "$CONFIG_DIR" ]; then
    rm -rf "$CONFIG_DIR"
    echo "   ✓ Removed: $CONFIG_DIR"
else
    echo "   ℹ  No config directory found — skipping"
fi

# ── 5. Remove legacy desktop autostart ───────────────────────────
if [ -f "$LEGACY_AUTOSTART" ]; then
    rm -f "$LEGACY_AUTOSTART"
    echo "   ✓ Removed legacy autostart entry"
fi

# ── 6. Disable linger (only if user wants it fully cleaned up) ────
echo ""
read -p "Disable loginctl linger for $REAL_USER? (removes startup-on-boot for ALL user services) (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    loginctl disable-linger "$REAL_USER" 2>/dev/null && echo "   ✓ Linger disabled" || \
        echo "   ⚠  Could not disable linger"
else
    echo "   ↳ Linger left unchanged"
fi

# ── 7. Done ───────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅ Uninstall Complete                               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Source code remains at: $SCRIPT_DIR"
echo "  To fully remove, run:   rm -rf $SCRIPT_DIR"
echo ""
