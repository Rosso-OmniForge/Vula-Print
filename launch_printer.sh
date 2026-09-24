#!/bin/bash
#
# Vula! Print Label Printer — Launcher
#
# Runs from a terminal or from systemd. Handles:
#   * .env loading (backend credentials)
#   * Display environment (X11 / Wayland) when systemd hasn't set it
#   * DBus session bus propagation (so Qt dialogs work)
#   * venv sanity check
#   * Singleton lock check (fast fail before forking Python)
#
# The app itself handles first-run onboarding (opens the connections
# dialog when no store is configured). This script never orchestrates
# setup — it just runs the app.

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── Local environment file ───────────────────────────────────────
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Display environment ──────────────────────────────────────────
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    WAYLAND_SOCK=$(ls /run/user/"$(id -u)"/wayland-* 2>/dev/null | head -1)
    if [ -n "$WAYLAND_SOCK" ]; then
        export WAYLAND_DISPLAY="$(basename "$WAYLAND_SOCK")"
    else
        X_DISPLAY=$(ls /tmp/.X11-unix/X* 2>/dev/null | head -1 | sed 's|/tmp/.X11-unix/X|:|')
        export DISPLAY="${X_DISPLAY:-:0}"
    fi
fi

# Propagate session bus so Qt dialogs work correctly
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    BUS_FILE="/run/user/$(id -u)/bus"
    [ -S "$BUS_FILE" ] && export DBUS_SESSION_BUS_ADDRESS="unix:path=$BUS_FILE"
fi

# ── Sanity checks ────────────────────────────────────────────────
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "ERROR: Virtual environment not found. Run: sudo bash install.sh" >&2
    exit 1
fi

# ── Singleton check ──────────────────────────────────────────────
LOCK_FILE="$HOME/.config/vula_print/app.lock"
if [ -e "$LOCK_FILE" ] && command -v flock >/dev/null 2>&1; then
    if ! flock --nonblock "$LOCK_FILE" true 2>/dev/null; then
        echo "Vula! Print already running — exiting." >&2
        exit 0
    fi
fi

# ── Launch ───────────────────────────────────────────────────────
source "$SCRIPT_DIR/venv/bin/activate"
exec python3 "$SCRIPT_DIR/vula_print_app.py"
