#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Vula! Print — single-command installer
#
# Phases (in execution order):
#   1. preflight  — Debian version, apt keyring, NTP sanity
#   2. system     — apt update/upgrade, firmware, base tools
#   3. display    — enforce X11 (required for AnyDesk unattended access)
#   4. security   — ufw, clamav, quad9 filtered DNS + probe
#   5. print_app  — venv, python deps, systemd user service, device ID
#   6. anydesk    — install, unattended password, UFW rules, handshake
#   7. firefox    — homepage policy + default browser
#   8. updater    — daily apt upgrade + daily git pull timers
#
# Usage:
#   sudo bash install.sh                    # everything, interactive
#   sudo bash install.sh --dry-run          # show what would happen
#   sudo bash install.sh --phase=anydesk    # one phase only
#   sudo bash install.sh --yes              # non-interactive
#   sudo bash install.sh \
#       --api-url=https://api.example.co.za \
#       --api-key=vp_... \
#       --store-url=https://store.example.co.za
#   sudo bash install.sh --no-ufw --no-clamav --no-dns --no-anydesk
#   sudo bash install.sh --apt-on-shutdown  # also upgrade on shutdown
#
# Full log: /var/log/vula-install-<timestamp>.log
# ════════════════════════════════════════════════════════════════

set -uo pipefail

# ── Argument parsing ─────────────────────────────────────────────
DRY_RUN=0
ASSUME_YES=0
PHASES_TO_RUN=""
STORE_URL=""
API_URL=""
API_KEY=""
SKIP_UFW=0
SKIP_CLAMAV=0
SKIP_DNS=0
SKIP_FIREFOX=0
SKIP_ANYDESK=0
SKIP_UPDATER=0
APT_ON_SHUTDOWN=0
ALLOW_SSH=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)          DRY_RUN=1 ;;
        --yes|-y)           ASSUME_YES=1 ;;
        --phase=*)          PHASES_TO_RUN="${arg#--phase=}" ;;
        --store-url=*)      STORE_URL="${arg#--store-url=}" ;;
        --api-url=*)        API_URL="${arg#--api-url=}" ;;
        --api-key=*)        API_KEY="${arg#--api-key=}" ;;
        --no-ufw)           SKIP_UFW=1 ;;
        --no-clamav)        SKIP_CLAMAV=1 ;;
        --no-dns)           SKIP_DNS=1 ;;
        --no-firefox)       SKIP_FIREFOX=1 ;;
        --no-anydesk)       SKIP_ANYDESK=1 ;;
        --no-updater)       SKIP_UPDATER=1 ;;
        --apt-on-shutdown)  APT_ON_SHUTDOWN=1 ;;
        --allow-ssh)        ALLOW_SSH=1 ;;
        --help|-h)
            awk '
                /^# ═{10,}/ { if (seen) exit; seen=1; next }
                seen { sub(/^# ?/, ""); print }
            ' "$0"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg (try --help)" >&2
            exit 1
            ;;
    esac
done

# ── Colours + log ────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

LOG_FILE="/var/log/vula-install-$(date +%Y%m%d-%H%M%S).log"
mkdir -p /var/log 2>/dev/null || true

# ── Logging via re-exec ────────────────────────────────────────────
# Do NOT use `exec > >(tee ...) 2>&1` here. That pattern leaves the
# tee subprocess attached to the invoking shell's job table, so the
# interactive prompt never returns after the script finishes.
# Re-exec through a proper pipeline instead — it closes cleanly.
if [ "${VULA_LOGGED:-0}" != "1" ]; then
    export VULA_LOGGED=1
    export VULA_ORIGINAL_LOG_FILE="$LOG_FILE"
    # Resolve $0 to an absolute path — invoking shell was `sudo bash install.sh`
    # so $0 is the relative string "install.sh", which is not a command.
    VULA_SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    export VULA_SELF
    "$VULA_SELF" "$@" 2>&1 | tee -a "$LOG_FILE"
    exit "${PIPESTATUS[0]}"
fi

# Child process: stdout is already going through the parent's tee.
# Adopt the parent's LOG_FILE so summary output shows the right path.
LOG_FILE="${VULA_ORIGINAL_LOG_FILE:-$LOG_FILE}"

trap 'rc=$?; echo; echo "── Log saved to: $LOG_FILE ──"; exit $rc' EXIT

# ── Helpers ──────────────────────────────────────────────────────
section() { echo; echo -e "${BOLD}${CYAN}══ $* ══${NC}"; }
info()    { echo -e "${CYAN}[*]${NC} $*"; }
ok()      { echo -e "${GREEN}[+ ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
err()     { echo -e "${RED}[-]${NC} $*"; }
dim()     { echo -e "${DIM}    $*${NC}"; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        dim "DRY: $*"
        return 0
    fi
    "$@"
}

# ask_yn "Question?" default(y/n) → returns 0 for yes
ask_yn() {
    local prompt="$1" default="${2:-n}" reply
    if [ "$ASSUME_YES" = "1" ]; then
        [ "$default" = "y" ] && return 0 || return 1
    fi
    local suffix="[y/N]"
    [ "$default" = "y" ] && suffix="[Y/n]"
    read -rp "$prompt $suffix " reply
    reply="${reply:-$default}"
    [[ "${reply,,}" == "y" ]]
}

_validate_url() {
    local url="${1:-}"
    if [ -z "$url" ]; then
        err "URL is empty."
        return 1
    fi
    if ! echo "$url" | grep -qE '^https?://[^[:space:]]+'; then
        err "Invalid URL: '$url'"
        err "  Must start with http:// or https://"
        return 1
    fi
    return 0
}

# ── Validate --phase value early (before anything else runs) ─────
if [ -n "$PHASES_TO_RUN" ]; then
    _VALID_PHASES=" preflight system display security print_app anydesk firefox updater "
    IFS=',' read -ra _REQUESTED_PHASES <<< "$PHASES_TO_RUN"
    for _req in "${_REQUESTED_PHASES[@]}"; do
        _req="${_req// /}"
        [ -z "$_req" ] && continue
        if [[ "$_VALID_PHASES" != *" $_req "* ]]; then
            echo "Unknown phase in --phase='$PHASES_TO_RUN': '$_req'" >&2
            echo "Valid phases: preflight system display security print_app anydesk firefox updater" >&2
            exit 1
        fi
    done
    unset _VALID_PHASES _REQUESTED_PHASES _req
fi

# ── Root + real-user detection ───────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    err "Run as root: sudo bash $0"
    exit 1
fi

if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    REAL_USER="$SUDO_USER"
else
    read -rp "${CYAN}[?]${NC} Username to run Vula Print as: " REAL_USER
fi

if ! id "$REAL_USER" &>/dev/null; then
    err "User '$REAL_USER' does not exist."
    exit 1
fi
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
REAL_UID=$(id -u "$REAL_USER")

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

as_user() {
    sudo -u "$REAL_USER" \
        env HOME="$REAL_HOME" \
            XDG_RUNTIME_DIR="/run/user/$REAL_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$REAL_UID/bus" \
        "$@"
}

# ── Banner ───────────────────────────────────────────────────────
echo
echo "╔════════════════════════════════════════════════════════╗"
echo "║   Vula! Print — Installer                             ║"
echo "╚════════════════════════════════════════════════════════╝"
echo
echo "  Installer  : $0"
echo "  Source dir : $SCRIPT_DIR"
echo "  Real user  : $REAL_USER ($REAL_HOME)"
echo "  Log file   : $LOG_FILE"
echo "  Dry run    : $([ "$DRY_RUN" = 1 ] && echo yes || echo no)"
echo

# ════════════════════════════════════════════════════════════════
# PHASE 1 — PREFLIGHT
# ════════════════════════════════════════════════════════════════
phase_preflight() {
    info "Verifying Debian release..."
    if [ ! -f /etc/debian_version ]; then
        err "Not a Debian system (/etc/debian_version missing)."
        return 1
    fi
    local codename
    codename=$(cat /etc/debian_version | cut -d. -f1)
    case "$codename" in
        13|trixie) ok "Debian 13 (Trixie) confirmed." ;;
        *) warn "Expected Trixie, found '$codename' — continuing anyway." ;;
    esac

    info "Verifying APT archive keyring..."
    if [ ! -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
        DEBIAN_FRONTEND=noninteractive run apt-get update -qq || return 1
        DEBIAN_FRONTEND=noninteractive run apt-get install -y debian-archive-keyring || return 1
    fi
    ok "APT archive keyring present."

    info "Checking for hardware clock / timezone sanity..."
    if ! timedatectl show --property=NTPSynchronized --value 2>/dev/null | grep -q yes; then
        warn "Clock is not NTP-synchronised. HTTPS handshakes may fail."
        warn "  Fix: timedatectl set-ntp true"
    fi

    ok "Preflight complete."
    return 0
}

# ════════════════════════════════════════════════════════════════
# PHASE 2 — SYSTEM
# ════════════════════════════════════════════════════════════════
phase_system() {
    info "Refreshing apt sources..."

    # Trixie can be expressed in two layouts:
    #   * legacy  /etc/apt/sources.list         (one-liner format)
    #   * deb822  /etc/apt/sources.list.d/*.sources
    # A clean Debian 13 netinst ships deb822. An upgraded box may still
    # have the legacy file. Handle both.
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then
        info "deb822 layout detected — rewriting debian.sources for Trixie."
        cat > /etc/apt/sources.list.d/debian.sources <<'DEB822_EOF'
Types: deb deb-src
URIs: http://deb.debian.org/debian/
Suites: trixie trixie-updates
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb deb-src
URIs: http://security.debian.org/debian-security/
Suites: trixie-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
DEB822_EOF
        : > /etc/apt/sources.list
        ok "debian.sources rewritten (legacy sources.list blanked)."
    elif grep -qE '^deb .* (bookworm|bullseye|buster)' /etc/apt/sources.list 2>/dev/null; then
        info "Legacy layout, old release — rewriting sources.list for Trixie."
        cat > /etc/apt/sources.list <<'SRCLIST_EOF'
# Debian 13 Trixie — managed by vula install.sh
deb     http://deb.debian.org/debian/            trixie          main contrib non-free non-free-firmware
deb-src http://deb.debian.org/debian/            trixie          main contrib non-free non-free-firmware
deb     http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
deb-src http://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware
deb     http://deb.debian.org/debian/            trixie-updates  main contrib non-free non-free-firmware
deb-src http://deb.debian.org/debian/            trixie-updates  main contrib non-free non-free-firmware
SRCLIST_EOF
        ok "sources.list rewritten."
    elif grep -qE '^deb .* trixie' /etc/apt/sources.list 2>/dev/null; then
        ok "sources.list already on Trixie — leaving untouched."
    else
        warn "Could not identify apt sources layout — leaving untouched."
        warn "  Verify manually: cat /etc/apt/sources.list /etc/apt/sources.list.d/*"
    fi

    if [ ! -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
        DEBIAN_FRONTEND=noninteractive apt-get install -y debian-archive-keyring || return 1
    fi

    run apt-get update -qq || return 1

    info "Installing base packages (this may take several minutes)..."
    DEBIAN_FRONTEND=noninteractive run apt-get install -y --no-install-recommends \
        sudo ca-certificates curl wget gnupg lsb-release \
        python3 python3-venv python3-pip python3-pyqt6 \
        libusb-1.0-0 git cups cups-client printer-driver-all \
        ufw clamav clamav-daemon clamav-freshclam \
        firefox-esr \
        unattended-upgrades apt-listchanges \
        apparmor apparmor-utils apparmor-profiles \
        fonts-noto-color-emoji \
        locales tzdata \
        || return 1
    ok "Base packages installed."

    info "Configuring locale (en_ZA.UTF-8 + en_US.UTF-8)..."
    sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen 2>/dev/null || true
    sed -i 's/^# *en_ZA.UTF-8/en_ZA.UTF-8/' /etc/locale.gen 2>/dev/null || true
    run locale-gen >/dev/null 2>&1
    ok "Locales generated."

    info "Adding '$REAL_USER' to hardware groups..."
    for grp in lp dialout lpadmin video audio; do
        if getent group "$grp" >/dev/null; then
            if ! id -nG "$REAL_USER" | grep -qw "$grp"; then
                run usermod -aG "$grp" "$REAL_USER"
                ok "  added to $grp"
            else
                dim "  already in $grp"
            fi
        fi
    done

    if [ ! -f /etc/udev/rules.d/60-usb-label-printer.rules ]; then
        cat > /etc/udev/rules.d/60-usb-label-printer.rules <<'UDEV_EOF'
SUBSYSTEM=="usb", KERNEL=="lp[0-9]*", GROUP="lp", MODE="0664"
UDEV_EOF
        run udevadm control --reload-rules
        run udevadm trigger --subsystem-match=usb
        ok "udev rule installed for printer device permissions."
    fi

    info "Enabling system services..."
    for svc in apparmor cups; do
        run systemctl enable --now "$svc" >/dev/null 2>&1 || warn "  could not enable $svc"
    done
    ok "System services enabled."

    return 0
}

# ════════════════════════════════════════════════════════════════
# PHASE 3 — X11 ENFORCEMENT
# ════════════════════════════════════════════════════════════════
phase_display() {
    section "Display — enforce X11"

    # 1. Move Wayland session files aside. This is the DM-agnostic way:
    #    GDM3, SDDM, and LightDM all read /usr/share/wayland-sessions/.
    if [ -d /usr/share/wayland-sessions ] && \
       ls /usr/share/wayland-sessions/*.desktop &>/dev/null; then
        info "Disabling Wayland session files..."
        mkdir -p /var/lib/vula/disabled-wayland-sessions
        mv /usr/share/wayland-sessions/*.desktop \
           /var/lib/vula/disabled-wayland-sessions/ 2>/dev/null || true
        ok "Wayland sessions disabled."
    else
        dim "  No Wayland sessions found (already disabled or not installed)."
    fi

    # 2. SDDM does not have a "force X11" config key. Its session list is
    #    built purely from /usr/share/xsessions/ + /usr/share/wayland-sessions/.
    #    Removing the Wayland session files above is sufficient.
    if command -v sddm >/dev/null 2>&1; then
        ok "SDDM detected — X11-only enforced via removed Wayland sessions."
    fi

    # 3. GDM3
    if [ -d /etc/gdm3 ]; then
        for f in /etc/gdm3/daemon.conf /etc/gdm3/custom.conf; do
            [ -f "$f" ] || continue
            if grep -qE '^#?\s*WaylandEnable' "$f"; then
                sed -i 's/^#\?\s*WaylandEnable=.*/WaylandEnable=false/' "$f"
            elif grep -q '^\[daemon\]' "$f"; then
                sed -i '/^\[daemon\]/a WaylandEnable=false' "$f"
            else
                printf '\n[daemon]\nWaylandEnable=false\n' >> "$f"
            fi
        done
        ok "GDM3 pinned to X11."
    fi

    # 4. LightDM
    if [ -f /etc/lightdm/lightdm.conf ]; then
        if grep -q '^\[Seat:\*\]' /etc/lightdm/lightdm.conf; then
            grep -q 'xserver-command' /etc/lightdm/lightdm.conf || \
                sed -i '/^\[Seat:\*\]/a xserver-command=X -core' /etc/lightdm/lightdm.conf
        fi
        ok "LightDM pinned to X11."
    fi

    # 5. Belt-and-braces: strip a stray WAYLAND_DISPLAY from login shells.
    #
    # NOTE: we deliberately do NOT export XDG_SESSION_TYPE=x11 here.
    # That variable is set by the display manager; overriding it in a
    # login shell (which includes SSH sessions with no X server) causes
    # more confusion than it prevents. The DM-level enforcement above is
    # the real fix; this profile.d file only removes the Wayland pointer
    # if someone lands in a shell that somehow inherited one.
    cat > /etc/profile.d/90-vula-x11.sh <<'PROFILE_EOF'
# Vula — this device is provisioned X11-only for AnyDesk unattended
# access. If a stray Wayland socket is present, ignore it.
if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -z "${DISPLAY:-}" ]; then
    unset WAYLAND_DISPLAY
fi
PROFILE_EOF
    chmod 644 /etc/profile.d/90-vula-x11.sh

    # 6. Report whether a reboot is pending.
    local cur="" sid
    sid=$(loginctl --no-legend list-sessions 2>/dev/null | awk '/seat/{print $1; exit}')
    [ -n "$sid" ] && cur=$(loginctl show-session "$sid" -p Type --value 2>/dev/null || echo "")
    if [ "$cur" = "wayland" ]; then
        warn "Active session is STILL Wayland — a reboot is REQUIRED"
        warn "before AnyDesk will work. Rebooting now is recommended."
        mkdir -p /var/lib
        touch /var/lib/vula-needs-reboot
    else
        ok "Active session type: ${cur:-unknown} (X11 enforcement in place)."
    fi
    return 0
}

# ════════════════════════════════════════════════════════════════
# PHASE 4 — SECURITY
# ════════════════════════════════════════════════════════════════
phase_security() {
    local rc=0

    # ── 4a. UFW ─────────────────────────────────────────────
    if [ "$SKIP_UFW" != "1" ]; then
        section "UFW firewall"
        info "Configuring UFW (deny incoming, allow outgoing)."

        if [ "$ASSUME_YES" != "1" ] && [ "$ALLOW_SSH" != "1" ]; then
            if ask_yn "  Allow SSH access on port 22 for remote support?" n; then
                ALLOW_SSH=1
            fi
        fi

        run ufw --force reset >/dev/null 2>&1 || true
        run ufw default deny incoming
        run ufw default allow outgoing
        [ "$ALLOW_SSH" = "1" ] && run ufw allow 22/tcp comment 'SSH' || true
        run ufw --force enable
        ok "UFW enabled (incoming: $( [ "$ALLOW_SSH" = "1" ] && echo 'SSH only' || echo 'blocked' ))"
    fi

    # ── 4b. DNS → Quad9 ────────────────────────────────────
    if [ "$SKIP_DNS" != "1" ]; then
        section "Filtered DNS (Quad9)"
        info "Configuring systemd-resolved to use Quad9 filtered DNS."

        mkdir -p /etc/systemd/resolved.conf.d
        cat > /etc/systemd/resolved.conf.d/90-vula-quad9.conf <<'DNS_EOF'
[Resolve]
DNS=9.9.9.11 149.112.112.11
FallbackDNS=9.9.9.9 149.112.112.112
DNSSEC=allow-downgrade
DNSOverTLS=opportunistic
Domains=~.
DNS_EOF

        run systemctl restart systemd-resolved >/dev/null 2>&1 || \
            warn "systemd-resolved not running — DNS changes will apply on next boot."

        sleep 1

        local probe_url="${STORE_URL:-}"
        if [ -n "$probe_url" ]; then
            local host
            host=$(echo "$probe_url" | sed -E 's#^https?://##; s#/.*##; s#:.*##')
            info "Probing $host through Quad9..."

            if command -v resolvectl >/dev/null; then
                resolvectl flush-caches >/dev/null 2>&1 || true
            fi

            if getent hosts "$host" >/dev/null 2>&1; then
                ok "  $host resolves via Quad9."
            else
                warn "  $host does NOT resolve via Quad9."
                warn "  This may be a private/internal hostname that only the"
                warn "  store's own DNS can resolve."

                if [ "$ASSUME_YES" != "1" ]; then
                    if ask_yn "  Revert to DHCP-provided DNS for this machine?" y; then
                        rm -f /etc/systemd/resolved.conf.d/90-vula-quad9.conf
                        systemctl restart systemd-resolved >/dev/null 2>&1 || true
                        ok "  Reverted to DHCP DNS."
                    else
                        warn "  Keeping Quad9 — store URL may not resolve."
                    fi
                fi
            fi
        else
            warn "No store URL — skipping DNS probe."
        fi

        ok "DNS phase done."
    fi

    # ── 4c. ClamAV ─────────────────────────────────────────
    if [ "$SKIP_CLAMAV" != "1" ]; then
        section "ClamAV"
        info "Enabling freshclam service (auto virus DB updates)..."
        run systemctl enable --now clamav-freshclam >/dev/null 2>&1 || \
            warn "Could not start clamav-freshclam."

        info "Installing daily scan timer (03:00)..."

        cat > /usr/local/bin/vula-clamscan.sh <<'CLAM_SCRIPT_EOF'
#!/bin/bash
# Daily ClamAV scan of the Vula POS user's home directory.
# Excludes caches, browser profiles, venvs, and the printer app's config.
set -u
LOG=/var/log/vula/clamscan.log
mkdir -p /var/log/vula

REAL_USER="${1:-}"
if [ -z "$REAL_USER" ] || ! id "$REAL_USER" &>/dev/null; then
    echo "$(date -Iseconds) ERROR: invalid user '$REAL_USER'" >> "$LOG"
    exit 1
fi
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)

if [ ! -d "$REAL_HOME" ]; then
    echo "$(date -Iseconds) ERROR: $REAL_HOME not found" >> "$LOG"
    exit 1
fi

{
    echo "════════════════════════════════════════════════════"
    echo "Scan started: $(date -Iseconds)"
    echo "Target:       $REAL_HOME"
    echo "════════════════════════════════════════════════════"
} >> "$LOG"

nice -n 19 ionice -c3 clamscan \
    --recursive=yes \
    --infected \
    --exclude-dir='^/proc' \
    --exclude-dir='^/sys' \
    --exclude-dir='^/dev' \
    --exclude-dir='^/run' \
    --exclude-dir='venv' \
    --exclude-dir='\.cache' \
    --exclude-dir='\.mozilla' \
    --exclude-dir='\.config/vula_print' \
    "$REAL_HOME" >> "$LOG" 2>&1

echo "Scan finished: $(date -Iseconds)" >> "$LOG"
CLAM_SCRIPT_EOF
        chmod 755 /usr/local/bin/vula-clamscan.sh

        cat > /etc/systemd/system/vula-clamscan.service <<CLAM_SVC_EOF
[Unit]
Description=Vula! Print daily ClamAV scan
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vula-clamscan.sh $REAL_USER
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=3600
CLAM_SVC_EOF

        cat > /etc/systemd/system/vula-clamscan.timer <<'CLAM_TIMER_EOF'
[Unit]
Description=Run Vula ClamAV scan daily at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
CLAM_TIMER_EOF

        run systemctl daemon-reload
        run systemctl enable --now vula-clamscan.timer >/dev/null 2>&1 || \
            warn "Could not start clamscan timer."
        ok "ClamAV scan scheduled (daily 03:00, logs to /var/log/vula/clamscan.log)"
    fi

    return "$rc"
}

# ════════════════════════════════════════════════════════════════
# PHASE 5 — PRINT APP
# ════════════════════════════════════════════════════════════════
phase_print_app() {
    local env_file="$SCRIPT_DIR/.env"

    if [ -z "$API_KEY" ] && [ -f "$env_file" ]; then
        API_KEY=$(grep -E '^PRINTER_API_KEY=' "$env_file" | tail -1 | cut -d= -f2-)
        [ -n "$API_KEY" ] && info "Using existing API key from .env"
    fi

    # Fall back: if --api-url wasn't given, allow --store-url to double
    # as the API base for backwards compatibility, with a warning.
    if [ -z "$API_URL" ]; then
        if [ -n "$STORE_URL" ]; then
            warn "--api-url not supplied; falling back to --store-url for the API base."
            API_URL="$STORE_URL"
        fi
    fi
    if [ -z "$API_URL" ]; then
        if [ "$ASSUME_YES" = "1" ]; then
            err "No --api-url (and no --store-url fallback) provided and --yes was set."
            return 1
        fi
        read -rp "${CYAN}[?]${NC} Vula backend API URL (e.g. https://api.example.co.za): " API_URL
    fi

    if [ -z "$API_KEY" ]; then
        if [ "$ASSUME_YES" = "1" ]; then
            err "No --api-key provided and --yes was set."
            return 1
        fi
        read -rsp "${CYAN}[?]${NC} Printer API key (vp_...): " API_KEY
        echo
    fi

    if [ -z "$API_URL" ] || [ -z "$API_KEY" ]; then
        err "Both API_URL and API_KEY are required."
        return 1
    fi

    _validate_url "$API_URL" || return 1
    if ! echo "$API_KEY" | grep -qE '^vp_'; then
        warn "API key does not start with 'vp_' — is that correct?"
        if [ "$ASSUME_YES" != "1" ]; then
            if ! ask_yn "  Continue anyway?" n; then
                return 1
            fi
        fi
    fi

    # Persist per-user .env — API base, NOT the storefront. The storefront
    # URL only feeds the Firefox homepage policy in phase_firefox.
    cat > "$env_file" <<ENV_EOF
# Vula! Print runtime environment — managed by install.sh
PRINTER_API_BASE_URL=$API_URL
PRINTER_API_KEY=$API_KEY
ENV_EOF
    chmod 600 "$env_file"
    chown "$REAL_USER":"$REAL_USER" "$env_file"
    ok "Credentials saved to $env_file"

    # Root-only copy for system services (AnyDesk handshake, updaters).
    # Not owned by the desktop user — the app reads .env, system services
    # read this. Same values, different ACLs.
    #
    # Use a subshell for the umask so it does not leak to later phases.
    install -d -m 0750 /etc/vula
    (
        umask 077
        cat > /etc/vula/creds.env <<CREDS_EOF
PRINTER_API_BASE_URL=$API_URL
PRINTER_API_KEY=$API_KEY
CREDS_EOF
    )
    chown root:root /etc/vula/creds.env
    chmod 0600 /etc/vula/creds.env
    ok "Root-only creds written to /etc/vula/creds.env"

    # Per-device stable identity. Do NOT rely on /etc/machine-id — it is
    # baked into the base image and identical across cloned devices.
    if [ ! -s /etc/vula/device-id ]; then
        if command -v uuidgen >/dev/null 2>&1; then
            uuidgen > /etc/vula/device-id
        else
            python3 -c "import uuid; print(uuid.uuid4())" > /etc/vula/device-id
        fi
        chmod 0644 /etc/vula/device-id
        chown root:root /etc/vula/device-id
        ok "Generated per-device ID: $(cat /etc/vula/device-id)"
    else
        dim "  Device ID already present: $(cat /etc/vula/device-id)"
    fi

    # ── venv ────────────────────────────────────────────────────
    if [ -d "$SCRIPT_DIR/venv" ]; then
        info "Existing venv found — reusing."
    else
        info "Creating venv..."
        run as_user python3 -m venv --system-site-packages "$SCRIPT_DIR/venv" || return 1
        ok "venv created."
    fi

    info "Installing Python dependencies..."
    run as_user "$SCRIPT_DIR/venv/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements_app.txt" || return 1
    ok "Python packages installed."

    # ── systemd user service ────────────────────────────────────
    local svc_file="$REAL_HOME/.config/systemd/user/vula-print.service"
    as_user mkdir -p "$REAL_HOME/.config/systemd/user"

    cat > "$svc_file" <<SVC_EOF
[Unit]
Description=Vula! Print Label Printer
After=graphical-session.target network-online.target
Wants=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStartPre=/bin/sleep 2
ExecStart=$SCRIPT_DIR/launch_printer.sh
Restart=on-failure
RestartSec=15
StartLimitIntervalSec=300
StartLimitBurst=3
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=10

[Install]
WantedBy=graphical-session.target
SVC_EOF
    chown "$REAL_USER":"$REAL_USER" "$svc_file"
    ok "Service file written."

    run loginctl enable-linger "$REAL_USER" >/dev/null 2>&1 || \
        warn "Could not enable linger (systemd too old?)"

    as_user systemctl --user daemon-reload
    as_user systemctl --user enable vula-print.service >/dev/null 2>&1
    as_user systemctl --user restart vula-print.service >/dev/null 2>&1 || true

    sleep 2
    if as_user systemctl --user is-active --quiet vula-print.service; then
        ok "vula-print.service is running."
    else
        warn "vula-print.service is not yet active — will start on next login."
    fi

    return 0
}

# ════════════════════════════════════════════════════════════════
# PHASE 6 — ANYDESK
# ════════════════════════════════════════════════════════════════
phase_anydesk() {
    section "AnyDesk remote access"

    if [ "$SKIP_ANYDESK" = "1" ]; then
        info "AnyDesk phase skipped (--no-anydesk)."
        return 0
    fi

    # 0a. Refuse to proceed on Wayland — unattended access will not work.
    local cur="" sid
    sid=$(loginctl --no-legend list-sessions 2>/dev/null | awk '/seat/{print $1; exit}')
    [ -n "$sid" ] && cur=$(loginctl show-session "$sid" -p Type --value 2>/dev/null || echo "")
    if [ "$cur" = "wayland" ]; then
        err "Session is Wayland. Reboot into X11 first, then re-run:"
        err "  sudo bash install.sh --phase=anydesk"
        return 1
    fi

    # 0b. The handshake service needs /etc/vula/creds.env, written by
    #     phase_print_app. If it isn't there, every retry cycle will be a
    #     no-op log spam. Refuse early with a clear instruction.
    if [ ! -s /etc/vula/creds.env ]; then
        err "Missing /etc/vula/creds.env — run the print_app phase first:"
        err "  sudo bash install.sh --phase=print_app"
        err "  sudo bash install.sh --phase=anydesk"
        return 1
    fi

    # 1. Signing key — HTTPS + dearmor, written into /etc/apt/keyrings.
    install -d -m 0755 /etc/apt/keyrings
    if [ ! -s /etc/apt/keyrings/anydesk.gpg ]; then
        info "Fetching AnyDesk signing key..."
        curl -fsSL https://keys.anydesk.com/repos/DEB-GPG-KEY \
            | gpg --dearmor -o /etc/apt/keyrings/anydesk.gpg || return 1
        chmod 0644 /etc/apt/keyrings/anydesk.gpg
    fi
    if ! gpg --show-keys --with-colons /etc/apt/keyrings/anydesk.gpg >/dev/null 2>&1; then
        err "AnyDesk keyring is not a valid GPG file."
        err "  rm /etc/apt/keyrings/anydesk.gpg and re-run."
        return 1
    fi
    ok "AnyDesk keyring present."

    # 2. Repo, pinned to that key.
    cat > /etc/apt/sources.list.d/anydesk-stable.list <<'ANYDESK_LIST'
deb [signed-by=/etc/apt/keyrings/anydesk.gpg] https://deb.anydesk.com/ all main
ANYDESK_LIST

    run apt-get update -qq || return 1
    DEBIAN_FRONTEND=noninteractive run apt-get install -y --no-install-recommends anydesk || return 1
    run systemctl enable --now anydesk || return 1

    # 3. Wait for the daemon to answer --get-id. Note this can take a
    #    moment on first install while AnyDesk registers with its cloud.
    info "Waiting for AnyDesk daemon..."
    local ad_id=""
    local i
    for i in $(seq 1 30); do
        ad_id=$(as_user anydesk --get-id 2>/dev/null | tr -dc '0-9' || true)
        [ -n "$ad_id" ] && break
        sleep 1
    done
    if [ -z "$ad_id" ]; then
        err "AnyDesk did not return an ID within 30s."
        err "  systemctl status anydesk"
        err "  sudo -u $REAL_USER anydesk --get-id"
        return 1
    fi
    ok "AnyDesk ID: $ad_id"

    # 4. Unattended password — 32 alphanumeric from /dev/urandom.
    #    Pull 128 bytes so the alphanumeric filter reliably yields ≥32 chars.
    local pw=""
    pw=$(head -c 128 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 32)
    if [ "${#pw}" -ne 32 ]; then
        err "Password generation produced ${#pw} chars (want 32)."
        return 1
    fi

    # AnyDesk CLI is inconsistent across versions: some prompt once, some
    # prompt twice for confirmation. Sending the password twice is safe in
    # both cases — the extra line is discarded when only one prompt fires.
    if ! printf '%s\n%s\n' "$pw" "$pw" | as_user anydesk --set-password >/dev/null 2>&1; then
        err "anydesk --set-password failed."
        err "  Verify manually: sudo -u $REAL_USER anydesk --set-password"
        return 1
    fi
    ok "Unattended password set."

    # 5. Belt-and-braces: also flag unattended access in user config.
    local ad_conf="$REAL_HOME/.anydesk/system.conf"
    as_user mkdir -p "$REAL_HOME/.anydesk"
    if [ -f "$ad_conf" ] && grep -q '^ad.anynet.unattended_access' "$ad_conf"; then
        sed -i 's/^ad.anynet.unattended_access.*/ad.anynet.unattended_access=1/' "$ad_conf"
    else
        echo 'ad.anynet.unattended_access=1' >> "$ad_conf"
    fi
    chown "$REAL_USER":"$REAL_USER" "$ad_conf" 2>/dev/null || true
    chmod 600 "$ad_conf" 2>/dev/null || true

    # 6. Persist ID + password for the handshake service (root-only).
    #    Wrap the write in a subshell so the restrictive umask does not
    #    leak into later phases.
    install -d -m 0750 /etc/vula
    (
        umask 077
        cat > /etc/vula/anydesk.json <<ANYDESK_JSON
{
  "anydesk_id": "$ad_id",
  "anydesk_password": "$pw",
  "set_at": "$(date -Iseconds)",
  "sent": false
}
ANYDESK_JSON
    )
    chown root:root /etc/vula/anydesk.json
    chmod 0600 /etc/vula/anydesk.json

    # 7. UFW rules for AnyDesk's inbound. Done here, AFTER security phase
    #    has already reset the firewall — otherwise they'd be wiped.
    if command -v ufw >/dev/null 2>&1; then
        run ufw allow 7070/tcp comment 'AnyDesk direct' || true
        run ufw allow 3478/udp comment 'AnyDesk STUN'   || true
        run ufw allow 3479/udp comment 'AnyDesk relay'  || true
        ok "UFW rules added for AnyDesk."
    fi

    # 8. Handshake helper + retry timer.
    cat > /usr/local/bin/vula-anydesk-handshake.sh <<'HS_SCRIPT'
#!/bin/bash
# Vula AnyDesk handshake — POSTs ID + password to the backend, retries
# via vula-anydesk-handshake.timer until the backend ACKs.
set -u
LOG=/var/log/vula/anydesk-handshake.log
mkdir -p /var/log/vula
JSON=/etc/vula/anydesk.json
CREDS=/etc/vula/creds.env

[ -f "$JSON" ]  || { echo "$(date -Iseconds) ERROR: $JSON missing"  >> "$LOG"; exit 1; }
[ -f "$CREDS" ] || { echo "$(date -Iseconds) ERROR: $CREDS missing" >> "$LOG"; exit 1; }

# Already sent? Nothing to do. The installer flips this to false on
# every phase_anydesk run, so a reinstall will resend with a fresh pw.
if python3 -c "import json,sys; sys.exit(0 if json.load(open('$JSON')).get('sent') else 1)" 2>/dev/null; then
    echo "$(date -Iseconds) already sent — nothing to do" >> "$LOG"
    exit 0
fi

# shellcheck disable=SC1090
. "$CREDS"
if [ -z "${PRINTER_API_BASE_URL:-}" ] || [ -z "${PRINTER_API_KEY:-}" ]; then
    echo "$(date -Iseconds) ERROR: creds.env incomplete" >> "$LOG"
    exit 1
fi

# Prefer the installer-generated per-device UUID. Fall back to machine-id
# only if the UUID file is missing (pre-existing installs).
serial=$(cat /etc/vula/device-id 2>/dev/null | tr -d '[:space:]')
if [ -z "$serial" ]; then
    serial=$(cat /etc/machine-id 2>/dev/null | tr -d '[:space:]')
    echo "$(date -Iseconds) WARN: device-id missing, falling back to machine-id" >> "$LOG"
fi
[ -z "$serial" ] && serial=$(hostname)

payload=$(python3 -c "
import json, sys
d = json.load(open('$JSON'))
print(json.dumps({
    'anydesk_id':       d['anydesk_id'],
    'anydesk_password': d['anydesk_password'],
    'serial':           sys.argv[1],
}))
" "$serial")

# NOTE: no -f on curl. We want the response body and HTTP code even on
# 4xx/5xx — otherwise every retry logs an opaque 'curl failed' and we
# cannot tell 404 (endpoint not deployed) from 401 (bad key).
resp=$(curl -sS -m 20 -w '\n%{http_code}' \
    -H "X-Printer-API-Key: $PRINTER_API_KEY" \
    -H "Content-Type: application/json" \
    -X POST "$PRINTER_API_BASE_URL/admin/api/printer-app/anydesk" \
    --data-raw "$payload" 2>&1) || {
    echo "$(date -Iseconds) curl network failure: ${resp:0:200}" >> "$LOG"
    exit 1
}

# Split response body / HTTP code on the last newline.
code="${resp##*$'\n'}"
body="${resp%$'\n'*}"

# Defensive: an empty response or a code that isn't all digits means
# the connection dropped mid-transfer.
if ! [[ "$code" =~ ^[0-9]{3}$ ]]; then
    echo "$(date -Iseconds) malformed response: code='$code' body='${body:0:200}'" >> "$LOG"
    exit 1
fi

if [ "$code" = "200" ] || [ "$code" = "201" ] || [ "$code" = "204" ]; then
    python3 - <<PY
import json, datetime
p = "$JSON"
d = json.load(open(p))
d["sent"]    = True
d["sent_at"] = datetime.datetime.now().isoformat()
json.dump(d, open(p, "w"), indent=2)
PY
    chmod 0600 "$JSON"
    ad_id_echo=$(python3 -c "import json;print(json.load(open('$JSON'))['anydesk_id'])")
    echo "$(date -Iseconds) handshake OK (anydesk_id=$ad_id_echo)" >> "$LOG"
    systemctl disable --now vula-anydesk-handshake.timer >/dev/null 2>&1 || true
    exit 0
fi

echo "$(date -Iseconds) handshake failed: HTTP $code body=${body:0:200}" >> "$LOG"
exit 1
HS_SCRIPT
    chmod 0755 /usr/local/bin/vula-anydesk-handshake.sh

    cat > /etc/systemd/system/vula-anydesk-handshake.service <<'HS_SVC'
[Unit]
Description=Vula AnyDesk handshake (retry until acknowledged)
After=network-online.target anydesk.service
Wants=network-online.target
ConditionFileNotEmpty=/etc/vula/anydesk.json
ConditionFileNotEmpty=/etc/vula/creds.env

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vula-anydesk-handshake.sh
HS_SVC

    cat > /etc/systemd/system/vula-anydesk-handshake.timer <<'HS_TIMER'
[Unit]
Description=Retry Vula AnyDesk handshake every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
HS_TIMER

    run systemctl daemon-reload
    run systemctl enable --now vula-anydesk-handshake.timer || \
        warn "Could not enable handshake retry timer."

    # 9. Try the handshake immediately; if it fails, the timer will retry.
    info "Attempting initial handshake..."
    if [ "$DRY_RUN" = "1" ]; then
        dim "DRY: /usr/local/bin/vula-anydesk-handshake.sh"
    elif /usr/local/bin/vula-anydesk-handshake.sh; then
        ok "Handshake acknowledged by backend."
    else
        warn "Initial handshake failed — retry timer will keep trying."
        warn "  tail -f /var/log/vula/anydesk-handshake.log"
    fi

    ok "AnyDesk phase complete."
    return 0
}

# ════════════════════════════════════════════════════════════════
# PHASE 7 — FIREFOX
# ════════════════════════════════════════════════════════════════
phase_firefox() {
    if [ "$SKIP_FIREFOX" = "1" ]; then
        info "Firefox phase skipped (--no-firefox)."
        return 0
    fi

    # Do NOT fall back to PRINTER_API_BASE_URL — the storefront and the API
    # are different hosts. If no --store-url was supplied, skip the policy
    # rather than pin Firefox to the wrong site.
    if [ -z "$STORE_URL" ]; then
        if [ "$ASSUME_YES" = "1" ]; then
            warn "No --store-url supplied — Firefox homepage policy will be skipped."
            warn "  Re-run with: sudo bash install.sh --phase=firefox --store-url=https://..."
            return 0
        fi
        read -rp "${CYAN}[?]${NC} Store URL to set as Firefox homepage: " STORE_URL
        [ -z "$STORE_URL" ] && { warn "Empty URL — skipping."; return 0; }
    fi

    if ! _validate_url "$STORE_URL"; then
        warn "Skipping Firefox homepage policy due to invalid URL."
        return 0
    fi

    info "Writing Firefox enterprise policies..."
    # Debian's firefox-esr reads from /etc/firefox-esr/policies/.
    # Mozilla's official build (non-Debian) reads from /etc/firefox/policies/.
    # Write to both so the policy applies regardless of which binary is
    # installed or which one a future apt upgrade switches to.
    mkdir -p /etc/firefox-esr/policies /etc/firefox/policies
    cat > /etc/firefox-esr/policies/policies.json <<FIREFOX_EOF
{
  "policies": {
    "Homepage": {
      "URL": "$STORE_URL",
      "StartPage": "homepage",
      "Locked": true
    },
    "OverrideFirstRunPage": "",
    "OverridePostUpdatePage": "",
    "DontCheckDefaultBrowser": true,
    "DisplayBookmarksToolbar": "never",
    "OfferToSaveLogins": false,
    "PasswordManagerEnabled": false,
    "DisableTelemetry": true,
    "DisableFirefoxStudies": true,
    "DisablePocket": true,
    "DisableFeedbackCommands": true,
    "SearchSuggestEnabled": false,
    "Extensions": {
      "Install": []
    }
  }
}
FIREFOX_EOF
    cp /etc/firefox-esr/policies/policies.json /etc/firefox/policies/policies.json

    info "Setting Firefox as the system default browser..."
    update-alternatives --set x-www-browser /usr/bin/firefox-esr 2>/dev/null || \
        warn "  could not set x-www-browser"
    update-alternatives --set gnome-www-browser /usr/bin/firefox-esr 2>/dev/null || \
        warn "  could not set gnome-www-browser"

    # Per-user XDG default — needs a session bus. Run under dbus-run-session
    # so it works even when the installer runs before first login.
    if command -v dbus-run-session >/dev/null 2>&1; then
        as_user dbus-run-session -- \
            xdg-settings set default-web-browser firefox-esr.desktop 2>/dev/null || \
            warn "  could not set per-user default browser (will apply on next login)"
    fi
    ok "Firefox homepage policy installed (locked to $STORE_URL)."
    return 0
}

# ════════════════════════════════════════════════════════════════
# PHASE 8 — AUTO-UPDATER
# ════════════════════════════════════════════════════════════════
phase_updater() {
    if [ "$SKIP_UPDATER" = "1" ]; then
        info "Updater phase skipped (--no-updater)."
        return 0
    fi

    mkdir -p /var/log/vula

    # ── 8a. Daily apt upgrade ───────────────────────────────
    info "Installing daily apt-upgrade timer (04:00)..."

    cat > /usr/local/bin/vula-apt-upgrade.sh <<'APTUPGRADE_SCRIPT'
#!/bin/bash
# Daily system upgrade for Vula POS boxes. Runs with low I/O priority
# so a print job in progress doesn't get starved.
set -u
LOG=/var/log/vula/apt-upgrade.log
mkdir -p /var/log/vula
{
    echo "════════════════════════════════════════════════════"
    echo "apt upgrade started: $(date -Iseconds)"
    echo "════════════════════════════════════════════════════"
} >> "$LOG"

export DEBIAN_FRONTEND=noninteractive
nice -n 19 ionice -c3 apt-get update -qq >> "$LOG" 2>&1
nice -n 19 ionice -c3 apt-get -y \
    -o Dpkg::Options::='--force-confdef' \
    -o Dpkg::Options::='--force-confold' \
    -o Acquire::Retries=3 \
    upgrade >> "$LOG" 2>&1
RC=$?

echo "apt upgrade finished: $(date -Iseconds) rc=$RC" >> "$LOG"
exit "$RC"
APTUPGRADE_SCRIPT
    chmod 755 /usr/local/bin/vula-apt-upgrade.sh

    cat > /etc/systemd/system/vula-apt-upgrade.service <<'APTSVC_EOF'
[Unit]
Description=Vula! Print daily apt upgrade
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vula-apt-upgrade.sh
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=1800
APTSVC_EOF

    cat > /etc/systemd/system/vula-apt-upgrade.timer <<'APTTIMER_EOF'
[Unit]
Description=Daily apt upgrade at 04:00

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
APTTIMER_EOF

    run systemctl daemon-reload
    run systemctl enable --now vula-apt-upgrade.timer >/dev/null 2>&1 || \
        warn "Could not start apt-upgrade timer."
    ok "apt upgrade scheduled (daily 04:00)."

    # ── 8b. Optional: on shutdown ──────────────────────────
    if [ "$APT_ON_SHUTDOWN" = "1" ]; then
        info "Adding on-shutdown apt upgrade service (30 min timeout)..."
        cat > /etc/systemd/system/vula-apt-on-shutdown.service <<'SHUTSVC_EOF'
[Unit]
Description=Vula! Print apt upgrade on shutdown
DefaultDependencies=no
After=network-online.target
Wants=network-online.target
Before=shutdown.target
Conflicts=reboot.target

[Service]
Type=oneshot
RemainAfterExit=true
ExecStop=/usr/local/bin/vula-apt-upgrade.sh
TimeoutStopSec=1800
KillMode=process

[Install]
WantedBy=multi-user.target
SHUTSVC_EOF
        run systemctl daemon-reload
        run systemctl enable vula-apt-on-shutdown.service >/dev/null 2>&1 || \
            warn "Could not enable on-shutdown service."
        ok "apt upgrade will also run on shutdown (30 min timeout)."
    fi

    # ── 8c. App auto-update ────────────────────────────────
    info "Installing app auto-update timer (04:05, runs as $REAL_USER)..."

    cat > /usr/local/bin/vula-print-update.sh <<'APPUP_SCRIPT'
#!/bin/bash
# Pull the latest Vula Print source from GitHub and restart the service
# if the HEAD commit changed. Refuses to run while a print job is in
# flight (signalled by /tmp/vula-print-busy).
set -u
LOG=/var/log/vula/app-update.log
mkdir -p /var/log/vula

REAL_USER="${1:-}"
if [ -z "$REAL_USER" ]; then
    echo "$(date -Iseconds) ERROR: no user passed" >> "$LOG"
    exit 1
fi
REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
SCRIPT_DIR="${2:-}"
if [ -z "$SCRIPT_DIR" ] || [ ! -d "$SCRIPT_DIR/.git" ]; then
    echo "$(date -Iseconds) ERROR: bad source dir '$SCRIPT_DIR'" >> "$LOG"
    exit 1
fi

if [ -e /tmp/vula-print-busy ]; then
    echo "$(date -Iseconds) SKIP: /tmp/vula-print-busy is set" >> "$LOG"
    exit 0
fi

cd "$SCRIPT_DIR" || exit 1

sudo -u "$REAL_USER" env HOME="$REAL_HOME" \
    XDG_RUNTIME_DIR="/run/user/$(id -u "$REAL_USER")" \
    git fetch --quiet origin || exit 1

LOCAL=$(sudo -u "$REAL_USER" git rev-parse HEAD)
REMOTE=$(sudo -u "$REAL_USER" git rev-parse origin/main 2>/dev/null \
      || sudo -u "$REAL_USER" git rev-parse origin/master 2>/dev/null)

if [ -z "$REMOTE" ]; then
    echo "$(date -Iseconds) ERROR: could not resolve origin/main" >> "$LOG"
    exit 1
fi

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "$(date -Iseconds) up-to-date at ${LOCAL:0:7}" >> "$LOG"
    exit 0
fi

echo "$(date -Iseconds) updating ${LOCAL:0:7} -> ${REMOTE:0:7}" >> "$LOG"

if ! sudo -u "$REAL_USER" git merge --ff-only "$REMOTE" >> "$LOG" 2>&1; then
    echo "$(date -Iseconds) ERROR: fast-forward merge failed" >> "$LOG"
    exit 1
fi

if [ -x "$SCRIPT_DIR/venv/bin/pip" ]; then
    sudo -u "$REAL_USER" "$SCRIPT_DIR/venv/bin/pip" install --quiet \
        -r "$SCRIPT_DIR/requirements_app.txt" >> "$LOG" 2>&1 || true
fi

sudo -u "$REAL_USER" env HOME="$REAL_HOME" \
    XDG_RUNTIME_DIR="/run/user/$(id -u "$REAL_USER")" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u "$REAL_USER")/bus" \
    systemctl --user restart vula-print.service >> "$LOG" 2>&1

echo "$(date -Iseconds) update complete; service restarted" >> "$LOG"
APPUP_SCRIPT
    chmod 755 /usr/local/bin/vula-print-update.sh

    cat > /etc/systemd/system/vula-print-update.service <<APPUPSVC_EOF
[Unit]
Description=Vula! Print app auto-update
After=network-online.target vula-apt-upgrade.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vula-print-update.sh $REAL_USER $SCRIPT_DIR
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=900
APPUPSVC_EOF

    cat > /etc/systemd/system/vula-print-update.timer <<'APPUPTIMER_EOF'
[Unit]
Description=Daily Vula Print source update at 04:05

[Timer]
OnCalendar=*-*-* 04:05:00
RandomizedDelaySec=5m
Persistent=true

[Install]
WantedBy=timers.target
APPUPTIMER_EOF

    run systemctl daemon-reload
    run systemctl enable --now vula-print-update.timer >/dev/null 2>&1 || \
        warn "Could not start app-update timer."
    ok "App auto-update scheduled (daily 04:05)."

    return 0
}

# ════════════════════════════════════════════════════════════════
# PHASE RUNNER
# ════════════════════════════════════════════════════════════════
run_phase() {
    local key="$1"
    local name="$2"
    local fn="$3"

    if [ -n "$PHASES_TO_RUN" ]; then
        if ! echo ",$PHASES_TO_RUN," | grep -q ",$key,"; then
            dim "Skipping phase '$key' (not in --phase=$PHASES_TO_RUN)"
            return 0
        fi
    fi

    section "Phase: $name"

    if [ "$DRY_RUN" = "1" ]; then
        dim "DRY RUN — would call $fn()"
        return 0
    fi

    if $fn; then
        ok "✓ Phase '$name' completed."
        return 0
    fi

    err "✗ Phase '$name' FAILED."

    if [ "$ASSUME_YES" = "1" ]; then
        err "  (--yes passed; aborting)"
        return 1
    fi

    if ask_yn "  Skip this phase and continue with the rest?" n; then
        warn "Skipping failed phase '$name' — continuing."
        return 0
    fi
    err "Aborting installer at operator request."
    return 1
}

# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════
FAILED=0

run_phase preflight "Preflight checks"          phase_preflight || FAILED=1
[ "$FAILED" = "0" ] && { run_phase system    "System preparation"       phase_system    || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase display   "X11 enforcement"          phase_display   || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase security  "Security hardening"       phase_security  || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase print_app "Print application"        phase_print_app || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase anydesk   "AnyDesk remote access"    phase_anydesk   || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase firefox   "Firefox provisioning"     phase_firefox   || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase updater   "Auto-updater setup"       phase_updater   || FAILED=1; }

# ── Summary ──────────────────────────────────────────────────
section "Installation Summary"
if [ "$FAILED" = "0" ]; then
    ok "All phases completed successfully."
else
    err "One or more phases failed — check the log for details."
fi
echo
echo "  Log file:  $LOG_FILE"
echo "  Restart:   systemctl --user restart vula-print    (as $REAL_USER)"
echo "  Status:    systemctl --user status vula-print"
echo "  App log:   journalctl --user -u vula-print -f"
echo "  Update:    sudo /usr/local/bin/vula-print-update.sh $REAL_USER $SCRIPT_DIR"
echo
echo "  Timers (system):"
echo "    systemctl list-timers vula-apt-upgrade.timer vula-clamscan.timer vula-print-update.timer vula-anydesk-handshake.timer"
echo

exit "$FAILED"