#!/bin/bash
#
# Vula! Print Label Printer — Launcher
# Works both from the terminal and as a systemd user service.
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# ── Local environment file ───────────────────────────────────────
# Loads PRINTER_API_KEY / PRINTER_API_BASE_URL / PRINTER_USER_ID
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
    set +a
fi

# ── Display environment ──────────────────────────────────────────
# When launched by systemd the DISPLAY / WAYLAND_DISPLAY variables may
# not be set yet.  We try three strategies in order:
#
#  1. Already set (normal terminal launch) → nothing to do.
#  2. Available via /run/user/<uid>/wayland-* or X11 socket.
#  3. Fallback to :0 (works for single-seat X11 installs).
#
if [ -z "$DISPLAY" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    # Try Wayland first
    WAYLAND_SOCK=$(ls /run/user/"$(id -u)"/wayland-* 2>/dev/null | head -1)
    if [ -n "$WAYLAND_SOCK" ]; then
        export WAYLAND_DISPLAY="$(basename "$WAYLAND_SOCK")"
    else
        # Fall back to X11 — find the display from any running Xorg/X process
        X_DISPLAY=$(ls /tmp/.X11-unix/X* 2>/dev/null | head -1 | sed 's|/tmp/.X11-unix/X|:|')
        export DISPLAY="${X_DISPLAY:-:0}"
    fi
fi

# Propagate session bus so Qt dialogs work correctly
if [ -z "$DBUS_SESSION_BUS_ADDRESS" ]; then
    BUS_FILE="/run/user/$(id -u)/bus"
    [ -S "$BUS_FILE" ] && export DBUS_SESSION_BUS_ADDRESS="unix:path=$BUS_FILE"
fi

# ── Sanity check ─────────────────────────────────────────────────
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "ERROR: Virtual environment not found. Run install_printer_app.sh first." >&2
    exit 1
fi

# ── Singleton check ──────────────────────────────────────────────
# If the app already holds its lock, refuse to launch a second copy.
# Uses the same lock file the Python side uses (~/.config/vula_print/app.lock).
LOCK_FILE="$HOME/.config/vula_print/app.lock"
if [ -e "$LOCK_FILE" ]; then
    if command -v flock >/dev/null 2>&1; then
        # `flock --nonblock` succeeds only if nothing holds the lock.
        if ! flock --nonblock "$LOCK_FILE" true 2>/dev/null; then
            echo "Vula! Print already running — exiting." >&2
            exit 0
        fi
    fi
fi

# ── Launch ───────────────────────────────────────────────────────
source "$SCRIPT_DIR/venv/bin/activate"

APP_CONFIG_FILE="$HOME/.config/vula_print/settings.json"

# First run / missing settings -> run the GUI onboarding wizard.
if [ ! -s "$APP_CONFIG_FILE" ]; then
    python3 "$SCRIPT_DIR/onboarding.py"
    ONBOARDING_EXIT_CODE=$?

    if [ "$ONBOARDING_EXIT_CODE" -ne 0 ]; then
        echo "Onboarding cancelled. Restart the service when ready." >&2
        exit 0
    fi

    # Give systemd a clean point after setup. The next service start
    # will go straight into the main app because settings now exist.
    exit 0
fi

# Use exec so the process inherits the PID systemd tracks.
# Any exit/crash causes systemd to apply the Restart= policy.
exec python3 "$SCRIPT_DIR/vula_print_app.py"
