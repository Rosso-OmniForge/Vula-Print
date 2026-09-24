#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Vula! Print — single-command installer
#
# Phases:
#   1. system     — apt update/upgrade, firmware, base tools
#   2. print_app  — venv, python deps, systemd user service
#   3. firefox    — homepage policy for the POS store URL
#   4. security   — ufw, clamav, quad9 filtered DNS + probe
#   5. updater    — daily apt upgrade + daily git pull timers
#
# Usage:
#   sudo bash install.sh                    # everything, interactive
#   sudo bash install.sh --dry-run          # show what would happen
#   sudo bash install.sh --phase=system     # one phase only
#   sudo bash install.sh --yes              # non-interactive
#   sudo bash install.sh --store-url=https://... --api-key=vp_...
#   sudo bash install.sh --no-ufw --no-clamav --no-dns
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
API_KEY=""
SKIP_UFW=0
SKIP_CLAMAV=0
SKIP_DNS=0
SKIP_FIREFOX=0
SKIP_UPDATER=0
APT_ON_SHUTDOWN=0
ALLOW_SSH=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)          DRY_RUN=1 ;;
        --yes|-y)           ASSUME_YES=1 ;;
        --phase=*)          PHASES_TO_RUN="${arg#--phase=}" ;;
        --store-url=*)      STORE_URL="${arg#--store-url=}" ;;
        --api-key=*)        API_KEY="${arg#--api-key=}" ;;
        --no-ufw)           SKIP_UFW=1 ;;
        --no-clamav)        SKIP_CLAMAV=1 ;;
        --no-dns)           SKIP_DNS=1 ;;
        --no-firefox)       SKIP_FIREFOX=1 ;;
        --no-updater)       SKIP_UPDATER=1 ;;
        --apt-on-shutdown)  APT_ON_SHUTDOWN=1 ;;
        --allow-ssh)        ALLOW_SSH=1 ;;
        --help|-h)
            sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
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
exec > >(tee -a "$LOG_FILE") 2>&1

section() { echo; echo -e "${BOLD}${CYAN}══ $* ══${NC}"; }
info()    { echo -e "${CYAN}[*]${NC} $*"; }
ok()      { echo -e "${GREEN}[+]${NC} $*"; }
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

# ── Root + real-user detection ───────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    err "Run as root: sudo bash $0"
    exit 1
fi

if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    REAL_USER="$SUDO_USER"
else
    # Fall back: ask which user the app should run as
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
# PHASE 1 — SYSTEM
# ════════════════════════════════════════════════════════════════
phase_system() {
    info "Refreshing apt sources..."

    # Rewrite sources.list only if it looks like stock Debian 12 or older
    if grep -qE '^deb .* bookworm|^deb .* bullseye|^deb .* buster' /etc/apt/sources.list 2>/dev/null; then
        info "Old release detected in sources.list — rewriting for Trixie."
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
    else
        ok "sources.list already correct — leaving untouched."
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
# PHASE 2 — PRINT APP
# ════════════════════════════════════════════════════════════════
phase_print_app() {
    # ── Collect credentials ─────────────────────────────────────
    local env_file="$SCRIPT_DIR/.env"

    if [ -z "$API_KEY" ] && [ -f "$env_file" ]; then
        API_KEY=$(grep -E '^PRINTER_API_KEY=' "$env_file" | tail -1 | cut -d= -f2-)
        [ -n "$API_KEY" ] && info "Using existing API key from .env"
    fi

    if [ -z "$STORE_URL" ]; then
        if [ "$ASSUME_YES" = "1" ]; then
            err "No --store-url provided and --yes was set."
            return 1
        fi
        read -rp "${CYAN}[?]${NC} Vula backend URL (e.g. https://shop.example.co.za): " STORE_URL
    fi

    if [ -z "$API_KEY" ]; then
        if [ "$ASSUME_YES" = "1" ]; then
            err "No --api-key provided and --yes was set."
            return 1
        fi
        read -rsp "${CYAN}[?]${NC} Printer API key (vp_...): " API_KEY
        echo
    fi

    if [ -z "$STORE_URL" ] || [ -z "$API_KEY" ]; then
        err "Both STORE_URL and API_KEY are required."
        return 1
    fi

    # Persist .env
    cat > "$env_file" <<ENV_EOF
# Vula! Print runtime environment — managed by install.sh
PRINTER_API_BASE_URL=$STORE_URL
PRINTER_API_KEY=$API_KEY
ENV_EOF
    chmod 600 "$env_file"
    chown "$REAL_USER":"$REAL_USER" "$env_file"
    ok "Credentials saved to $env_file"

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
# PHASE 3 — FIREFOX
# ════════════════════════════════════════════════════════════════
phase_firefox() {
    if [ "$SKIP_FIREFOX" = "1" ]; then
        info "Firefox phase skipped (--no-firefox)."
        return 0
    fi

    if [ -z "$STORE_URL" ]; then
        if [ -n "${PRINTER_API_BASE_URL:-}" ]; then
            STORE_URL="$PRINTER_API_BASE_URL"
        elif [ -f "$SCRIPT_DIR/.env" ]; then
            STORE_URL=$(grep -E '^PRINTER_API_BASE_URL=' "$SCRIPT_DIR/.env" | tail -1 | cut -d= -f2-)
        fi
    fi

    if [ -z "$STORE_URL" ]; then
        if [ "$ASSUME_YES" = "1" ]; then
            warn "No store URL available — skipping Firefox homepage policy."
            return 0
        fi
        read -rp "${CYAN}[?]${NC} Store URL to set as Firefox homepage: " STORE_URL
        [ -z "$STORE_URL" ] && { warn "Empty URL — skipping."; return 0; }
    fi

    info "Writing Firefox enterprise policies..."
    mkdir -p /etc/firefox/policies
    cat > /etc/firefox/policies/policies.json <<FIREFOX_EOF
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
    ok "Firefox homepage policy installed (locked to $STORE_URL)."
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

        # Interactive: ask about SSH unless auto-yes or explicit flag
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

        # Allow resolved to settle
        sleep 1

        # ── Probe the store URL through the new DNS ────────
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
# Excludes caches, browser profiles, venvs, and the printer app's config
# (which we control and don't need to scan).
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
# PHASE 5 — AUTO-UPDATER
# ════════════════════════════════════════════════════════════════
phase_updater() {
    if [ "$SKIP_UPDATER" = "1" ]; then
        info "Updater phase skipped (--no-updater)."
        return 0
    fi

    mkdir -p /var/log/vula

    # ── 5a. Daily apt upgrade ───────────────────────────────
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

    # ── 5b. Optional: on shutdown ──────────────────────────
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

    # ── 5c. App auto-update ────────────────────────────────
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

# Skip if a print job is currently in flight
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

# Fast-forward only — never force
if ! sudo -u "$REAL_USER" git merge --ff-only "$REMOTE" >> "$LOG" 2>&1; then
    echo "$(date -Iseconds) ERROR: fast-forward merge failed" >> "$LOG"
    exit 1
fi

# Refresh deps, in case requirements_app.txt changed
if [ -x "$SCRIPT_DIR/venv/bin/pip" ]; then
    sudo -u "$REAL_USER" "$SCRIPT_DIR/venv/bin/pip" install --quiet \
        -r "$SCRIPT_DIR/requirements_app.txt" >> "$LOG" 2>&1 || true
fi

# Restart user service
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

    # Filter by --phase if provided
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

run_phase system    "System preparation"       phase_system    || FAILED=1
[ "$FAILED" = "0" ] && { run_phase print_app "Print application"      phase_print_app || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase firefox   "Firefox provisioning"   phase_firefox   || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase security  "Security hardening"     phase_security  || FAILED=1; }
[ "$FAILED" = "0" ] && { run_phase updater   "Auto-updater setup"     phase_updater   || FAILED=1; }

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
echo "    systemctl list-timers vula-apt-upgrade.timer vula-clamscan.timer vula-print-update.timer"
echo

exit "$FAILED"
