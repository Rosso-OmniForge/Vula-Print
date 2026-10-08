#!/bin/bash
#
# Vula! Print Label Printer — Uninstall Script
#
# Usage:
#   bash uninstall.sh               # remove user-level state only
#   sudo bash uninstall.sh          # same, cleans up linger properly
#   sudo bash uninstall.sh --purge  # also remove every system-level file
#                                   # written by install.sh
#
# --purge removes /etc/vula/, /etc/systemd/system/vula-*.{service,timer},
# /usr/local/bin/vula-*.sh, the AnyDesk apt repo, the Quad9 DNS override,
# the Firefox enterprise policy, the X11 profile script, the udev rule,
# /var/log/vula/, /var/lib/vula/, and reverses the group memberships.
# It does NOT uninstall the anydesk apt package, and it does NOT reset
# UFW — those are separate, more destructive operations.
#

# ── Argument parsing ──────────────────────────────────────────────
PURGE=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --purge)  PURGE=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --help|-h)
            sed -n '2,18p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg (try --help)" >&2
            exit 1
            ;;
    esac
done

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
if [ "$PURGE" = "1" ]; then
    echo "  --purge is set. Additionally:"
    echo "    • /etc/vula/  /var/log/vula/  /var/lib/vula/"
    echo "    • /etc/systemd/system/vula-*.{service,timer}"
    echo "    • /usr/local/bin/vula-*.sh"
    echo "    • AnyDesk apt repo + signing key"
    echo "    • Quad9 DNS override, Firefox policy, X11 profile script"
    echo "    • udev rule 60-usb-label-printer.rules"
    echo "    • group memberships (lp, dialout, lpadmin, video, audio)"
    echo ""
    echo "  --purge does NOT remove the anydesk package itself,"
    echo "  and does NOT reset the UFW firewall."
    echo ""
fi
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
if [ "$PURGE" = "1" ] || [ "$ASSUME_YES" = "1" ]; then
    loginctl disable-linger "$REAL_USER" 2>/dev/null && \
        echo "   ✓ Linger disabled" || \
        echo "   ⚠  Could not disable linger"
else
    read -p "Disable loginctl linger for $REAL_USER? (removes startup-on-boot for ALL user services) (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        loginctl disable-linger "$REAL_USER" 2>/dev/null && echo "   ✓ Linger disabled" || \
            echo "   ⚠  Could not disable linger"
    else
        echo "   ↳ Linger left unchanged"
    fi
fi

# ── 6b. Purge system-level files (--purge only) ───────────────────
if [ "$PURGE" = "1" ]; then
    echo ""
    echo "🗑  Purging system-level files written by install.sh…"

    if [ "$EUID" -ne 0 ]; then
        echo "   ⚠  --purge requires root to remove /etc and /usr/local/bin"
        echo "      files. Re-run:  sudo bash $0 --purge"
    else
        # Stop + remove every system-level timer/service install.sh wrote.
        systemctl disable --now \
            vula-clamscan.timer \
            vula-apt-upgrade.timer \
            vula-print-update.timer \
            vula-anydesk-handshake.timer \
            vula-apt-on-shutdown.service \
            >/dev/null 2>&1 || true

        rm -f /etc/systemd/system/vula-clamscan.service \
              /etc/systemd/system/vula-clamscan.timer \
              /etc/systemd/system/vula-apt-upgrade.service \
              /etc/systemd/system/vula-apt-upgrade.timer \
              /etc/systemd/system/vula-print-update.service \
              /etc/systemd/system/vula-print-update.timer \
              /etc/systemd/system/vula-anydesk-handshake.service \
              /etc/systemd/system/vula-anydesk-handshake.timer \
              /etc/systemd/system/vula-apt-on-shutdown.service
        systemctl daemon-reload >/dev/null 2>&1 || true
        echo "   ✓ Removed systemd system units"

        rm -f /usr/local/bin/vula-clamscan.sh \
              /usr/local/bin/vula-apt-upgrade.sh \
              /usr/local/bin/vula-anydesk-handshake.sh \
              /usr/local/bin/vula-print-update.sh
        echo "   ✓ Removed /usr/local/bin/vula-*.sh"

        rm -f /etc/apt/sources.list.d/anydesk-stable.list \
              /etc/apt/keyrings/anydesk.gpg
        echo "   ✓ Removed AnyDesk apt repo + signing key"

        rm -f /etc/systemd/resolved.conf.d/90-vula-quad9.conf
        systemctl restart systemd-resolved >/dev/null 2>&1 || true
        echo "   ✓ Removed Quad9 DNS override"

        rm -f /etc/firefox-esr/policies/policies.json \
              /etc/firefox/policies/policies.json
        echo "   ✓ Removed Firefox enterprise policy"

        rm -f /etc/profile.d/90-vula-x11.sh
        echo "   ✓ Removed X11 enforcement profile script"

        rm -f /etc/udev/rules.d/60-usb-label-printer.rules
        udevadm control --reload-rules >/dev/null 2>&1 || true
        udevadm trigger >/dev/null 2>&1 || true
        echo "   ✓ Removed udev rule"

        rm -rf /etc/vula
        echo "   ✓ Removed /etc/vula (creds, device-id, anydesk state)"

        rm -rf /var/log/vula
        echo "   ✓ Removed /var/log/vula"

        rm -rf /var/lib/vula
        rm -f /var/lib/vula-needs-reboot
        echo "   ✓ Removed /var/lib/vula and reboot marker"

        for grp in lp dialout lpadmin video audio; do
            gpasswd -d "$REAL_USER" "$grp" >/dev/null 2>&1 || true
        done
        echo "   ✓ Removed $REAL_USER from printer/device groups"

        echo ""
        echo "   ⚠  AnyDesk package itself was NOT uninstalled. To remove:"
        echo "        sudo apt-get remove --purge anydesk"
        echo "   ⚠  UFW rules were NOT reset. To reset:"
        echo "        sudo ufw reset"
    fi
fi

# ── 7. Done ───────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅ Uninstall Complete                               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Source code remains at: $SCRIPT_DIR"
if [ "$PURGE" = "1" ] && [ "$EUID" -eq 0 ]; then
    echo "  System-level files purged. User data removed."
else
    echo "  To fully remove, run:   sudo bash $0 --purge"
fi
echo ""
