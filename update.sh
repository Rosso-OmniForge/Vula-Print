#!/bin/bash
#
# Vula! Print Label Printer — Update Script
#
# Usage (either form works):
#   bash update.sh
#   sudo bash update.sh
#

set -e

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

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Vula! Print Label Printer — Updater               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. Pull latest code ───────────────────────────────────────────
echo "🌐 Fetching latest version from GitHub…"
as_user git fetch origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main 2>/dev/null || git rev-parse origin/master)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "   ✓ Already up-to-date ($(git rev-parse --short HEAD))"
else
    echo "   ↳ Updating from $(git rev-parse --short HEAD) → $(git rev-parse --short "$REMOTE")"
    as_user git pull --rebase origin "$(git rev-parse --abbrev-ref HEAD)"
    echo "   ✓ Code updated"
fi

# ── 2. Refresh Python dependencies ───────────────────────────────
echo ""
echo "🐍 Refreshing Python dependencies…"
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "   ↳ venv not found — creating (this takes ~10 seconds)…"
    as_user python3 -m venv --system-site-packages "$SCRIPT_DIR/venv"
fi

echo "   ↳ PyQt6 comes from system apt — only lightweight packages are checked."
as_user "$SCRIPT_DIR/venv/bin/pip" install -r "$SCRIPT_DIR/requirements_app.txt"
echo "   ✓ Dependencies up-to-date"

# ── 3. Ensure correct ownership ───────────────────────────────────
if [ "$EUID" -eq 0 ]; then
    chown -R "$REAL_USER":"$REAL_USER" "$SCRIPT_DIR"
fi

# ── 4. Refresh service file (in case install.sh changed the unit) ─
if [ -f "$REAL_HOME/.config/systemd/user/${SERVICE_NAME}.service" ]; then
    echo ""
    echo "⚙  Reloading systemd unit…"
    as_user systemctl --user daemon-reload
    echo "   ✓ Daemon reloaded"
fi

# ── 5. Restart service ────────────────────────────────────────────
echo ""
echo "🔄 Restarting service…"
if as_user systemctl --user is-enabled --quiet "${SERVICE_NAME}.service" 2>/dev/null; then
    as_user systemctl --user restart "${SERVICE_NAME}.service"
    sleep 1
    if as_user systemctl --user is-active --quiet "${SERVICE_NAME}.service"; then
        echo "   ✓ Service restarted and RUNNING"
    else
        echo "   ⚠  Service did not become active — check:"
        echo "      journalctl --user -u ${SERVICE_NAME} -n 30"
    fi
else
    echo "   ℹ  Service not installed — run install_printer_app.sh first."
fi

# ── 6. Done ───────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅ Update Complete!                                 ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Running commit: $(git rev-parse --short HEAD)"
echo "  Date:           $(git log -1 --format='%cd' --date=short)"
echo ""
