#!/bin/bash
#
# Vula! Print — Uninstall
#
# Usage:
#   sudo bash uninstall.sh                # remove everything (keeps source dir)
#   sudo bash uninstall.sh --nuke         # also remove the source dir
#   sudo bash uninstall.sh --keep-config  # preserve /etc/vula* and settings
#   sudo bash uninstall.sh --yes          # non-interactive
#   sudo bash uninstall.sh --dry-run      # preview — shows what would happen
#   sudo bash uninstall.sh --help
#
# What this removes:
#   * All system-scope units   (vula-print-update, vula-apt-upgrade,
#     vula-clamscan, vula-anydesk-handshake, vula-update-manual,
#     vula-update-local, vula-apt-on-shutdown)
#   * The user-scope vula-print.service for the console user
#   * /usr/local/bin/vula-*.sh helper scripts
#   * /etc/vula*, /var/log/vula*, /var/lib/vula* (unless --keep-config)
#   * The udev rule, group memberships, loginctl linger
#   * The app's user config and print history
#
# What this does NOT remove (deliberately):
#   * The source directory (unless --nuke is passed)
#   * The anydesk apt package — run 'sudo apt-get remove --purge anydesk'
#   * UFW rules — run 'sudo ufw reset'
#
# Exit codes:
#   0 — success, or dry-run completed
#   1 — fatal setup error (no console user, bad args, --nuke without root)
#
# Dry-run mode:
#   Read-only queries (systemctl is-active, [ -f path ], loginctl show-user)
#   run for real so the preview reflects actual state. Anything that would
#   mutate the system is echoed with a DRY: prefix and skipped, and success
#   messages are downgraded to "(dry-run) ..." so nothing ever claims to
#   have been removed.

set -uo pipefail

# ── Colours ──────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

section() { echo; echo -e "${BOLD}${CYAN}══ $* ══${NC}"; }
info()    { echo -e "${CYAN}[*]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
err()     { echo -e "${RED}[-]${NC} $*"; }
dim()     { echo -e "${DIM}    $*${NC}"; }

# ── Args (parsed before anything uses DRY_RUN) ───────────────────
NUKE=0
KEEP_CONFIG=0
ASSUME_YES=0
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --nuke)         NUKE=1 ;;
        --keep-config)  KEEP_CONFIG=1 ;;
        --yes|-y)       ASSUME_YES=1 ;;
        --dry-run)      DRY_RUN=1 ;;
        --help|-h)
            sed -n '3,36p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg (try --help)" >&2
            exit 1
            ;;
    esac
done

# ── Output helpers (dry-run aware) ───────────────────────────────
# run() executes or short-circuits with a DRY: preview.
# ok() prints a green success marker, or a dim "(dry-run)" note —
# never the green marker during a preview, so we never lie about
# having changed the system.
run() {
    if [ "$DRY_RUN" = "1" ]; then
        dim "DRY: $*"
        return 0
    fi
    "$@"
}

ok() {
    if [ "$DRY_RUN" = "1" ]; then
        echo -e "${DIM}    (dry-run) $*${NC}"
    else
        echo -e "${GREEN}[+ ]${NC} $*"
    fi
}

# ── Root check ───────────────────────────────────────────────────
# Unprivileged runs are supported for user-scope cleanup only. A
# few operations genuinely require root — fail fast on those.
IS_ROOT=0
[ "$EUID" -eq 0 ] && IS_ROOT=1

if [ "$NUKE" = "1" ] && [ "$IS_ROOT" != "1" ]; then
    err "--nuke requires root (removing $SOURCE_DIR needs privileges)."
    exit 1
fi

# ── Resolve REAL_USER ────────────────────────────────────────────
# Handles three invocation patterns:
#   1. sudo bash uninstall.sh           → SUDO_USER is set
#   2. su -; bash uninstall.sh          → SUDO_USER empty; try install.env,
#                                         then loginctl, then source-dir owner
#   3. bash uninstall.sh (as a user)    → use $USER
if [ "$IS_ROOT" = "1" ]; then
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        REAL_USER="$SUDO_USER"
    elif [ -f /etc/vula/install.env ]; then
        ENV_USER=$(. /etc/vula/install.env && echo "${REAL_USER:-}")
        if [ -n "$ENV_USER" ] && id "$ENV_USER" &>/dev/null; then
            REAL_USER="$ENV_USER"
        fi
    fi

    if [ -z "${REAL_USER:-}" ]; then
        # su workflow — find the active console user
        CONSOLE_USER=$(loginctl list-sessions --no-legend 2>/dev/null \
            | awk '$3=="seat" {print $4; exit}')
        if [ -z "$CONSOLE_USER" ] && [ -f /etc/vula/source-dir ]; then
            SD=$(cat /etc/vula/source-dir 2>/dev/null | tr -d '\n')
            [ -n "$SD" ] && [ -d "$SD" ] && \
                CONSOLE_USER=$(stat -c '%U' "$SD" 2>/dev/null)
        fi
        if [ -z "$CONSOLE_USER" ]; then
            err "Could not detect the console user."
            err "Set it explicitly:  REAL_USER=pos sudo -E bash $0"
            exit 1
        fi
        REAL_USER="$CONSOLE_USER"
    fi
else
    REAL_USER="$USER"
fi

if ! id "$REAL_USER" &>/dev/null; then
    err "User '$REAL_USER' does not exist."
    exit 1
fi

REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
REAL_UID=$(id -u "$REAL_USER")

# ── Resolve SOURCE_DIR ───────────────────────────────────────────
# Prefer install.env (current convention since v1.1.87). Fall back to
# the legacy /etc/vula/source-dir file for older installs, then to the
# directory this script itself lives in.
SOURCE_DIR=""
if [ -f /etc/vula/install.env ]; then
    SOURCE_DIR=$(. /etc/vula/install.env && echo "${SOURCE_DIR:-}")
fi
if [ -z "$SOURCE_DIR" ] && [ -f /etc/vula/source-dir ]; then
    SOURCE_DIR=$(cat /etc/vula/source-dir 2>/dev/null | tr -d '\n')
fi
if [ -z "$SOURCE_DIR" ] || [ ! -d "$SOURCE_DIR" ]; then
    SOURCE_DIR="$(cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd)"
fi

as_user() {
    if [ "$IS_ROOT" = "1" ]; then
        sudo -u "$REAL_USER" \
            env HOME="$REAL_HOME" \
                XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$REAL_UID/bus" \
            "$@"
    else
        "$@"
    fi
}

# ── Banner ───────────────────────────────────────────────────────
echo
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Vula! Print — Uninstall                            ║"
echo "╚══════════════════════════════════════════════════════╝"
echo
echo "  Real user  : $REAL_USER  ($REAL_HOME)"
echo "  Source dir : $SOURCE_DIR"
echo "  Running as : $([ "$IS_ROOT" = 1 ] && echo root || echo "$REAL_USER (limited)")"
echo "  Dry run    : $([ "$DRY_RUN" = 1 ] && echo yes || echo no)"
echo
echo "  This will remove:"
echo "    • All system-scope systemd units (vula-*)"
echo "    • The user-scope vula-print.service"
echo "    • /usr/local/bin/vula-*.sh"
echo "    • /etc/vula/, /var/log/vula/, /var/lib/vula/"
echo "    • /etc/vula-print/, /var/log/vula-print/, /var/lib/vula-print/"
echo "    • Udev rule, group memberships, linger"
echo "    • App config and print history"
echo
if [ "$KEEP_CONFIG" = "1" ]; then
    echo "  --keep-config is set: /etc/vula* and /var/lib/vula-print settings"
    echo "  will be PRESERVED. Only services + units + scripts are removed."
    echo
fi
if [ "$NUKE" = "1" ]; then
    echo "  --nuke is set: $SOURCE_DIR will also be removed."
    echo
fi
if [ "$IS_ROOT" != "1" ]; then
    echo "  ⚠  Not running as root — only user-scope services and files"
    echo "     will be removed. Re-run with sudo for a full uninstall."
    echo
fi
echo "  ⚠  Does NOT remove the anydesk package or reset UFW."
echo "     Run those separately if you want them gone."
echo

if [ "$DRY_RUN" != "1" ] && [ "$ASSUME_YES" != "1" ]; then
    read -rp "Proceed with uninstall? (y/N) " -n 1 -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# ════════════════════════════════════════════════════════════════
# 1. Stop and disable USER-scope units
# ════════════════════════════════════════════════════════════════
section "Stopping user services"

for unit in vula-print.service; do
    if as_user systemctl --user is-active --quiet "$unit" 2>/dev/null; then
        run as_user systemctl --user stop "$unit" && ok "stopped $unit"
    fi
    if as_user systemctl --user is-enabled --quiet "$unit" 2>/dev/null; then
        run as_user systemctl --user disable "$unit" >/dev/null 2>&1 && \
            ok "disabled $unit"
    fi
    run as_user systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
done

run as_user systemctl --user daemon-reload >/dev/null 2>&1 || true

# ════════════════════════════════════════════════════════════════
# 2. Stop and disable SYSTEM-scope units
# ════════════════════════════════════════════════════════════════
if [ "$IS_ROOT" = "1" ]; then
    section "Stopping system services"

    SYSTEM_UNITS=(
        vula-print-update.timer
        vula-print-update.service
        vula-apt-upgrade.timer
        vula-apt-upgrade.service
        vula-clamscan.timer
        vula-clamscan.service
        vula-anydesk-handshake.timer
        vula-anydesk-handshake.service
        vula-apt-on-shutdown.service
        vula-update-manual.service
        vula-update-local.service
    )

    for unit in "${SYSTEM_UNITS[@]}"; do
        # stop is a no-op if not running, but always attempt — clears
        # some odd states where is-active lies
        run systemctl stop "$unit" >/dev/null 2>&1 || true
        if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            run systemctl disable "$unit" >/dev/null 2>&1 && \
                ok "disabled $unit"
        fi
        run systemctl reset-failed "$unit" >/dev/null 2>&1 || true
    done

    run systemctl daemon-reload >/dev/null 2>&1 || true
    ok "System-scope units stopped and disabled."
else
    section "System services (skipped)"
    warn "Not running as root — system-scope units not touched."
    warn "To remove them:  sudo bash $0"
fi

# ════════════════════════════════════════════════════════════════
# 3. Remove unit files
# ════════════════════════════════════════════════════════════════
section "Removing unit files"

USER_UNIT_FILE="$REAL_HOME/.config/systemd/user/vula-print.service"
if [ -f "$USER_UNIT_FILE" ]; then
    run rm -f "$USER_UNIT_FILE" && ok "removed $USER_UNIT_FILE"
fi
run as_user systemctl --user daemon-reload >/dev/null 2>&1 || true

if [ "$IS_ROOT" = "1" ]; then
    for f in /etc/systemd/system/vula-*.service /etc/systemd/system/vula-*.timer; do
        [ -e "$f" ] || continue
        run rm -f "$f" && ok "removed $f"
    done
    run systemctl daemon-reload >/dev/null 2>&1 || true
fi

# ════════════════════════════════════════════════════════════════
# 4. Remove /usr/local/bin scripts
# ════════════════════════════════════════════════════════════════
if [ "$IS_ROOT" = "1" ]; then
    section "Removing helper scripts"
    for f in /usr/local/bin/vula-*.sh; do
        [ -e "$f" ] || continue
        run rm -f "$f" && ok "removed $f"
    done
fi

# ════════════════════════════════════════════════════════════════
# 5. Remove config / logs / state
# ════════════════════════════════════════════════════════════════
section "Removing config, logs and state"

if [ "$KEEP_CONFIG" != "1" ] && [ "$IS_ROOT" = "1" ]; then
    for d in /etc/vula /etc/vula-print; do
        [ -d "$d" ] && run rm -rf "$d" && ok "removed $d"
    done
fi

if [ "$IS_ROOT" = "1" ]; then
    for d in /var/log/vula /var/log/vula-print; do
        [ -d "$d" ] && run rm -rf "$d" && ok "removed $d"
    done
fi

if [ "$KEEP_CONFIG" != "1" ] && [ "$IS_ROOT" = "1" ]; then
    [ -d /var/lib/vula ] && run rm -rf /var/lib/vula && \
        ok "removed /var/lib/vula"
    [ -d /var/lib/vula-print ] && run rm -rf /var/lib/vula-print && \
        ok "removed /var/lib/vula-print"
fi

# Reboot marker (always safe to remove)
if [ -f /var/lib/vula-needs-reboot ]; then
    run rm -f /var/lib/vula-needs-reboot && \
        ok "removed /var/lib/vula-needs-reboot"
fi

# User config
CONFIG_DIR="$REAL_HOME/.config/vula_print"
if [ -d "$CONFIG_DIR" ]; then
    run rm -rf "$CONFIG_DIR" && ok "removed $CONFIG_DIR"
fi

# Legacy autostart
LEGACY="$REAL_HOME/.config/autostart/vula-print.desktop"
if [ -f "$LEGACY" ]; then
    run rm -f "$LEGACY" && ok "removed legacy autostart entry"
fi

# ════════════════════════════════════════════════════════════════
# 6. Remove udev rule
# ════════════════════════════════════════════════════════════════
if [ "$IS_ROOT" = "1" ]; then
    section "Removing udev rule"
    if [ -f /etc/udev/rules.d/60-usb-label-printer.rules ]; then
        run rm -f /etc/udev/rules.d/60-usb-label-printer.rules && \
            ok "removed 60-usb-label-printer.rules"
        run udevadm control --reload-rules >/dev/null 2>&1 || true
        run udevadm trigger >/dev/null 2>&1 || true
    else
        dim "no udev rule found"
    fi
fi

# ════════════════════════════════════════════════════════════════
# 7. Revert anydesk-specific system files
# ════════════════════════════════════════════════════════════════
if [ "$IS_ROOT" = "1" ]; then
    section "Removing AnyDesk integration files"

    if [ -f /etc/apt/sources.list.d/anydesk-stable.list ]; then
        run rm -f /etc/apt/sources.list.d/anydesk-stable.list && \
            ok "removed AnyDesk apt repo"
    fi

    if [ -f /etc/apt/keyrings/anydesk.gpg ]; then
        run rm -f /etc/apt/keyrings/anydesk.gpg && \
            ok "removed AnyDesk signing key"
    fi
fi

# ════════════════════════════════════════════════════════════════
# 8. Revert DNS / Firefox / X11 tweaks
# ════════════════════════════════════════════════════════════════
if [ "$IS_ROOT" = "1" ]; then
    section "Removing system tweaks"

    if [ -f /etc/systemd/resolved.conf.d/90-vula-quad9.conf ]; then
        run rm -f /etc/systemd/resolved.conf.d/90-vula-quad9.conf
        run systemctl restart systemd-resolved >/dev/null 2>&1 || true
        ok "removed Quad9 DNS override"
    fi

    for f in /etc/firefox-esr/policies/policies.json \
             /etc/firefox/policies/policies.json; do
        if [ -f "$f" ]; then
            run rm -f "$f" && ok "removed $f"
        fi
    done

    if [ -f /etc/profile.d/90-vula-x11.sh ]; then
        run rm -f /etc/profile.d/90-vula-x11.sh && \
            ok "removed X11 enforcement profile script"
    fi
fi

# ════════════════════════════════════════════════════════════════
# 9. Group memberships
# ════════════════════════════════════════════════════════════════
if [ "$IS_ROOT" = "1" ]; then
    section "Reverting group memberships"
    for grp in lp dialout lpadmin video audio; do
        if id -nG "$REAL_USER" 2>/dev/null | grep -qw "$grp"; then
            run gpasswd -d "$REAL_USER" "$grp" >/dev/null 2>&1 && \
                ok "removed $REAL_USER from $grp"
        fi
    done
fi

# ════════════════════════════════════════════════════════════════
# 10. Linger
# ════════════════════════════════════════════════════════════════
if [ "$IS_ROOT" = "1" ]; then
    section "Disabling linger"
    if loginctl show-user "$REAL_USER" -p Linger --value 2>/dev/null | grep -q yes; then
        run loginctl disable-linger "$REAL_USER" >/dev/null 2>&1 && \
            ok "linger disabled for $REAL_USER"
    else
        dim "linger already disabled"
    fi
fi

# ════════════════════════════════════════════════════════════════
# 11. Optionally remove source
# ════════════════════════════════════════════════════════════════
if [ "$NUKE" = "1" ]; then
    section "Removing source directory"
    if [ -n "$SOURCE_DIR" ] && [ -d "$SOURCE_DIR" ]; then
        # Don't rm -rf the directory we're executing from out from under
        # ourselves. Linux allows it (inode is held open), but it's
        # confusing if we then try to write the log file into it.
        if [ "$(cd "$SOURCE_DIR" && pwd)" = "$(pwd)" ]; then
            cd /tmp
        fi
        run rm -rf "$SOURCE_DIR" && ok "removed $SOURCE_DIR"
    fi
fi

# ════════════════════════════════════════════════════════════════
# Done
# ════════════════════════════════════════════════════════════════
echo
if [ "$DRY_RUN" = "1" ]; then
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   ℹ  Dry run complete — nothing was changed          ║"
    echo "╚══════════════════════════════════════════════════════╝"
else
    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   ✅ Uninstall complete                              ║"
    echo "╚══════════════════════════════════════════════════════╝"
fi
echo

if [ "$IS_ROOT" != "1" ]; then
    echo "  ⚠  Rerun with sudo to remove system services and files."
fi

# Verify nothing's left registered — skipped in dry-run because nothing
# was actually removed, so the check would always report leftovers.
if [ "$DRY_RUN" != "1" ] && [ "$IS_ROOT" = "1" ]; then
    LEFT=$(systemctl list-unit-files 'vula-*' --no-legend 2>/dev/null | \
           awk '{print $1}' | tr '\n' ' ')
    if [ -n "$LEFT" ]; then
        echo "  ⚠  Still registered with systemd: $LEFT"
        echo "     This should be empty. Check: systemctl list-units 'vula-*'"
    else
        echo "  ✓ No vula-* units registered with systemd."
    fi
fi

echo
echo "  Manual followups (if you want them gone):"
echo "    sudo apt-get remove --purge anydesk    # AnyDesk package"
echo "    sudo ufw reset                         # firewall rules"
echo