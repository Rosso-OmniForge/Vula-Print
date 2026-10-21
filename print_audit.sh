#!/bin/bash
# ════════════════════════════════════════════════════════════════
# Vula! Print — diagnostic audit script
#
# Read-only. Collects system, install, printer, app, and update
# state into a single tarball for shipping back to the dev team.
#
# Usage:
#   sudo bash print_audit.sh              # standard collection
#   sudo bash print_audit.sh --full       # also capture dmesg/lsusb
#   sudo bash print_audit.sh --output=/tmp
#   sudo bash print_audit.sh --help
#
# Output: vula-audit-<hostname>-<timestamp>.tar.gz (in $PWD or --output)
#
# All API keys, passwords, and credentials are REDACTED. Safe to email.
# ════════════════════════════════════════════════════════════════
set -uo pipefail

FULL=0
OUTPUT_DIR="$(pwd)"
for arg in "$@"; do
    case "$arg" in
        --full) FULL=1 ;;
        --output=*) OUTPUT_DIR="${arg#--output=}" ;;
        --help|-h)
            sed -n '3,20p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Unknown argument: $arg (try --help)" >&2; exit 1 ;;
    esac
done

if [ "$EUID" -ne 0 ]; then
    echo "Run as root:  sudo bash $0" >&2
    exit 1
fi

TS=$(date +%Y%m%d-%H%M%S)
HOST=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown)
TARBALL="$OUTPUT_DIR/vula-audit-${HOST}-${TS}.tar.gz"
WORKDIR=$(mktemp -d -t vula-audit.XXXXXX)
trap 'rm -rf "$WORKDIR"' EXIT

mkdir -p "$WORKDIR/logs" "$WORKDIR/config"

# ── Helpers ─────────────────────────────────────────────────────
have() { command -v "$1" >/dev/null 2>&1; }
emit() { echo; echo "── $* ──"; }

# Redactor: masks common credential patterns in log/config output.
# Applied to every file that MIGHT contain a secret before archiving.
redact_stream() {
    sed -E \
        -e 's/vp_[A-Za-z0-9_-]{8,}/vp_***REDACTED***/g' \
        -e 's/("api_key"[[:space:]]*:[[:space:]]*")[^"]*/\1***REDACTED***/g' \
        -e 's/("anydesk_password"[[:space:]]*:[[:space:]]*")[^"]*/\1***REDACTED***/g' \
        -e 's/("PRINTER_API_KEY"[[:space:]]*[:=][[:space:]]*"?)[^"[:space:]]*/\1***REDACTED***/g' \
        -e 's/(X-Printer-API-Key[:=][[:space:]]*)[^[:space:]]+/\1***REDACTED***/g' \
        -e 's/(PRINTER_API_KEY=)[^[:space:]]+/\1***REDACTED***/g' \
        -e 's/(Bearer[[:space:]]+)[A-Za-z0-9_.\-]{8,}/\1***REDACTED***/g'
}

# Resolve operator user from install.env (authoritative), else SUDO_USER.
REAL_USER=""
if [ -f /etc/vula/install.env ]; then
    REAL_USER=$(grep -E '^REAL_USER=' /etc/vula/install.env | tail -1 | cut -d= -f2-)
fi
[ -z "$REAL_USER" ] && REAL_USER="${SUDO_USER:-}"
REAL_HOME=""
[ -n "$REAL_USER" ] && REAL_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
SOURCE_DIR=""
if [ -f /etc/vula/install.env ]; then
    SOURCE_DIR=$(grep -E '^SOURCE_DIR=' /etc/vula/install.env | tail -1 | cut -d= -f2-)
fi

# ════════════════════════════════════════════════════════════════
# 01 — SYSTEM
# ════════════════════════════════════════════════════════════════
{
    emit "Audit generated"
    date -Iseconds
    echo "hostname: $HOST"
    echo "euid:     $EUID"
    echo "real_user: ${REAL_USER:-<unknown>}"
    echo "source_dir: ${SOURCE_DIR:-<unknown>}"

    emit "OS"
    cat /etc/os-release 2>/dev/null
    uname -a

    emit "Uptime / load"
    uptime

    emit "Hardware"
    nproc
    free -h
    lscpu 2>/dev/null | head -20

    emit "Disks"
    df -h / /home /var /tmp 2>/dev/null

    emit "Time sync"
    timedatectl status 2>/dev/null

    emit "Virtualization"
    systemd-detect-virt 2>/dev/null || echo "(unknown)"
} > "$WORKDIR/01-system.txt" 2>&1

# ════════════════════════════════════════════════════════════════
# 02 — INSTALL STATE
# ════════════════════════════════════════════════════════════════
{
    emit "VERSION file"
    if [ -n "$SOURCE_DIR" ] && [ -f "$SOURCE_DIR/VERSION" ]; then
        cat "$SOURCE_DIR/VERSION"
    else
        echo "(VERSION not found — SOURCE_DIR=${SOURCE_DIR:-<unset>})"
    fi

    emit "/etc/vula/install.env"
    if [ -f /etc/vula/install.env ]; then
        ls -la /etc/vula/install.env
        cat /etc/vula/install.env
    else
        echo "MISSING"
    fi

    emit "/etc/vula/creds.env (presence + perms only; values REDACTED)"
    if [ -f /etc/vula/creds.env ]; then
        ls -la /etc/vula/creds.env
        grep -E '^[A-Z_]+=' /etc/vula/creds.env | \
            sed -E 's/^([A-Z_]+)=.+$/\1=<set>/;s/^([A-Z_]+)=$/\1=<empty>/'
    else
        echo "MISSING"
    fi

    emit "/etc/vula/ device-id"
    [ -f /etc/vula/device-id ] && cat /etc/vula/device-id || echo "MISSING"

    emit "/etc/vula/anydesk.json (presence only; password REDACTED)"
    if [ -f /etc/vula/anydesk.json ]; then
        ls -la /etc/vula/anydesk.json
        python3 -c "
import json
try:
    d = json.load(open('/etc/vula/anydesk.json'))
    print(f\"anydesk_id:       {d.get('anydesk_id','<unset>')}\")
    print(f\"password set:     {'yes' if d.get('anydesk_password') else 'no'}\")
    print(f\"set_at:           {d.get('set_at','<unset>')}\")
    print(f\"sent (acked):     {d.get('sent',False)}\")
    print(f\"sent_at:          {d.get('sent_at','<unset>')}\")
except Exception as e:
    print(f'(parse failed: {e})')
" 2>&1
    else
        echo "MISSING"
    fi

    emit "/etc/vula/log-upload-disabled"
    [ -f /etc/vula/log-upload-disabled ] && \
        { ls -la /etc/vula/log-upload-disabled; echo "(log upload DISABLED at install)"; } || \
        echo "(not present — log upload governed by UI toggle)"

    emit "/etc/vula/ directory"
    ls -la /etc/vula/ 2>/dev/null || echo "MISSING"

    emit "Installed binaries"
    ls -la /usr/local/bin/vula-* 2>/dev/null || echo "none"

    emit "Systemd units"
    ls -la /etc/systemd/system/vula-*.service /etc/systemd/system/vula-*.timer 2>/dev/null || echo "none"

    emit "Polkit rules"
    ls -la /etc/polkit-1/rules.d/ 2>/dev/null
    [ -f /etc/polkit-1/rules.d/49-vula-update.rules ] && \
        cat /etc/polkit-1/rules.d/49-vula-update.rules

    emit "Udev rules"
    [ -f /etc/udev/rules.d/60-usb-label-printer.rules ] && \
        cat /etc/udev/rules.d/60-usb-label-printer.rules || echo "MISSING"
} > "$WORKDIR/02-install.txt" 2>&1

# ════════════════════════════════════════════════════════════════
# 03 — PRINTER DEVICES
# ════════════════════════════════════════════════════════════════
{
    emit "USB printer-class (/dev/usb/lp*)"
    if ls /dev/usb/lp* >/dev/null 2>&1; then
        for d in /dev/usb/lp*; do
            echo "device: $d"
            stat -c '  perms: %A  owner: %U  group: %G' "$d"
            if have udevadm; then
                echo "  udev path: $(udevadm info -q path -n "$d" 2>/dev/null || echo '?')"
                udevadm info -q property -n "$d" 2>/dev/null | \
                    grep -E '^(ID_VENDOR|ID_MODEL|ID_SERIAL|DEVPATH|SUBSYSTEM)=' | sed 's/^/    /'
            fi
            if [ -e "/sys/class/usbmisc/$(basename "$d")" ]; then
                echo "  sysfs: /sys/class/usbmisc/$(basename "$d")  (modern)"
            fi
            if [ -e "/sys/class/usb/$(basename "$d")" ]; then
                echo "  sysfs: /sys/class/usb/$(basename "$d")  (legacy)"
            fi
        done
    else
        echo "none present"
    fi

    emit "Serial devices"
    for pat in /dev/ttyUSB* /dev/ttyACM* /dev/ttyS[0-9]*; do
        for d in $pat; do
            [ -e "$d" ] || continue
            echo "device: $d"
            stat -c '  perms: %A  owner: %U  group: %G' "$d"
        done
    done
    if ! ls /dev/ttyUSB* /dev/ttyACM* >/dev/null 2>&1; then
        echo "none present"
    fi

    emit "Serial by-id symlinks"
    ls -la /dev/serial/by-id/ 2>/dev/null || echo "no by-id directory"

    emit "usblp kernel module"
    lsmod | grep -E '^usblp' || echo "(usblp not loaded — some USB printers need it)"

    emit "CUPS queues"
    if have lpstat; then
        lpstat -p 2>&1 || echo "(lpstat -p failed)"
        echo
        echo "── CUPS queue details ──"
        lpstat -l -p 2>&1 | head -60
        echo
        echo "── CUPS default ──"
        lpstat -d 2>&1
    else
        echo "(lpstat not installed — CUPS unavailable)"
    fi

    emit "Group memberships for operator user"
    if [ -n "$REAL_USER" ]; then
        id "$REAL_USER"
        id -nG "$REAL_USER"
    else
        echo "(REAL_USER unknown)"
    fi
} > "$WORKDIR/03-printers.txt" 2>&1

# ════════════════════════════════════════════════════════════════
# 04 — USER SESSION
# ════════════════════════════════════════════════════════════════
{
    emit "Operator user"
    if [ -n "$REAL_USER" ]; then
        getent passwd "$REAL_USER"
        id "$REAL_USER"
    else
        echo "(REAL_USER unknown)"
    fi

    emit "Active login sessions"
    if have loginctl; then
        loginctl list-sessions --no-legend 2>/dev/null | while read -r sid _ _ _ _; do
            echo "session: $sid"
            loginctl show-session "$sid" -p Type -p User -p Active -p State 2>/dev/null | sed 's/^/  /'
        done
    fi

    emit "Graphical session type"
    if have loginctl; then
        _sid=$(loginctl --no-legend list-sessions 2>/dev/null | awk '/seat/{print $1; exit}')
        if [ -n "${_sid:-}" ]; then
            _type=$(loginctl show-session "$_sid" -p Type --value 2>/dev/null)
            echo "session $_sid: type=$_type"
            [ "$_type" = "wayland" ] && echo "  ⚠ Wayland detected — AnyDesk unattended access needs X11"
        fi
    fi

    emit "User runtime dir"
    if [ -n "$REAL_USER" ]; then
        UID_N=$(id -u "$REAL_USER")
        ls -ld "/run/user/$UID_N" 2>/dev/null || echo "MISSING /run/user/$UID_N"
    fi
} > "$WORKDIR/04-user.txt" 2>&1

# ════════════════════════════════════════════════════════════════
# 05 — APP STATE
# ════════════════════════════════════════════════════════════════
{
    emit "vula-print.service status"
    if [ -n "$REAL_USER" ]; then
        UID_N=$(id -u "$REAL_USER")
        sudo -u "$REAL_USER" \
            env HOME="$REAL_HOME" \
                XDG_RUNTIME_DIR="/run/user/$UID_N" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$UID_N/bus" \
            systemctl --user status vula-print.service --no-pager -l 2>&1 | head -40
    fi

    emit "vula-print.service unit file"
    [ -f "$REAL_HOME/.config/systemd/user/vula-print.service" ] && \
        cat "$REAL_HOME/.config/systemd/user/vula-print.service" || echo "MISSING"

    emit "Recent service journal (last 100 lines)"
    if [ -n "$REAL_USER" ]; then
        journalctl --user -u vula-print.service --no-pager -n 100 2>/dev/null || \
            echo "(journalctl user scope unavailable from sudo context)"
    fi

    emit "Config directory"
    [ -d "$REAL_HOME/.config/vula_print" ] && \
        ls -la "$REAL_HOME/.config/vula_print/" || echo "MISSING"

    emit "settings.json (redacted)"
    [ -f "$REAL_HOME/.config/vula_print/settings.json" ] && \
        cat "$REAL_HOME/.config/vula_print/settings.json" | redact_stream || \
        echo "MISSING"

    emit "print_jobs.db (summary queries)"
    if [ -f "$REAL_HOME/.config/vula_print/print_jobs.db" ] && have sqlite3; then
        ls -la "$REAL_HOME/.config/vula_print/print_jobs.db"*
        echo
        echo "── status counts ──"
        sqlite3 "$REAL_HOME/.config/vula_print/print_jobs.db" \
            "SELECT status, COUNT(*) FROM print_jobs GROUP BY status ORDER BY status" 2>&1
        echo
        echo "── doc_class counts ──"
        sqlite3 "$REAL_HOME/.config/vula_print/print_jobs.db" \
            "SELECT doc_class, COUNT(*) FROM print_jobs GROUP BY doc_class" 2>&1
        echo
        echo "── last 5 jobs ──"
        sqlite3 "$REAL_HOME/.config/vula_print/print_jobs.db" \
            "SELECT id, connection_name, doc_class, request_id, status, COALESCE(finished_at,fetched_at) FROM print_jobs ORDER BY id DESC LIMIT 5" 2>&1
        echo
        echo "── connections (api_key omitted) ──"
        sqlite3 "$REAL_HOME/.config/vula_print/print_jobs.db" \
            "SELECT connection_id, display_name, api_base_url, printer_user_id FROM connections" 2>&1
    else
        echo "MISSING or sqlite3 not installed"
    fi

    emit "Log directory perms"
    if [ -d "$REAL_HOME/.config/vula_print/logs" ]; then
        ls -la "$REAL_HOME/.config/vula_print/logs/"
    else
        echo "MISSING"
    fi

    emit "Busy file"
    if [ -f /tmp/vula-print-busy ]; then
        stat -c '  perms: %A  mtime: %y  age: %Y' /tmp/vula-print-busy
        age=$(( $(date +%s) - $(stat -c %Y /tmp/vula-print-busy 2>/dev/null || echo 0) ))
        echo "  age seconds: $age"
        [ "$age" -gt 3600 ] && echo "  ⚠ stale (>1h) — will be auto-removed on next update run"
    else
        echo "not present"
    fi
} > "$WORKDIR/05-app.txt" 2>&1

# ════════════════════════════════════════════════════════════════
# 06 — UPDATES
# ════════════════════════════════════════════════════════════════
{
    emit "Vula timers"
    systemctl list-timers --all 2>/dev/null | grep -E 'vula-|NEXT' | head -15

    for t in vula-apt-upgrade vula-clamscan vula-print-update vula-anydesk-handshake; do
        emit "$t.timer status"
        systemctl status "$t.timer" --no-pager -l 2>&1 | head -10 || echo "(not installed)"
        echo
        systemctl status "$t.service" --no-pager -l 2>&1 | head -10 || true
    done

    emit "vula-update.sh identity"
    if [ -f /usr/local/bin/vula-update.sh ]; then
        ls -la /usr/local/bin/vula-update.sh
        md5sum /usr/local/bin/vula-update.sh
        [ -n "$SOURCE_DIR" ] && [ -f "$SOURCE_DIR/system/bin/vula-update.sh" ] && {
            echo "repo copy:"
            md5sum "$SOURCE_DIR/system/bin/vula-update.sh"
            echo
            echo "diff (installed vs repo):"
            diff /usr/local/bin/vula-update.sh "$SOURCE_DIR/system/bin/vula-update.sh" 2>&1 | head -40 || true
        }
    else
        echo "MISSING"
    fi

    emit "Update state files"
    [ -f /var/lib/vula/update-failure-count ] && \
        { echo -n "update-failure-count: "; cat /var/lib/vula/update-failure-count; } || \
        echo "update-failure-count: 0 (no file — clean)"
    [ -f /var/lib/vula/last-known-good ] && \
        { echo -n "last-known-good: "; cat /var/lib/vula/last-known-good; } || \
        echo "last-known-good: (no file)"
    ls -la /var/lib/vula/ 2>/dev/null || echo "(no /var/lib/vula)"

    emit "Git state"
    if [ -n "$SOURCE_DIR" ] && [ -d "$SOURCE_DIR/.git" ] && [ -n "$REAL_USER" ]; then
        echo "HEAD:              $(sudo -u "$REAL_USER" git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null)"
        echo "current branch:    $(sudo -u "$REAL_USER" git -C "$SOURCE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)"
        echo "origin/HEAD:       $(sudo -u "$REAL_USER" git -C "$SOURCE_DIR" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || echo '(unset)')"
        echo
        echo "── remote refs ──"
        sudo -u "$REAL_USER" git -C "$SOURCE_DIR" branch -r 2>&1
        echo
        echo "── last 5 commits ──"
        sudo -u "$REAL_USER" git -C "$SOURCE_DIR" log --oneline -5 2>&1
        echo
        echo "── working tree status ──"
        sudo -u "$REAL_USER" git -C "$SOURCE_DIR" status --porcelain 2>&1 | head -20
    else
        echo "(SOURCE_DIR not a git checkout or REAL_USER unknown)"
    fi

    emit "Recent update.log (last 100 lines, redacted)"
    [ -f /var/log/vula/update.log ] && \
        tail -100 /var/log/vula/update.log | redact_stream || \
        echo "(no update.log)"
} > "$WORKDIR/06-updates.txt" 2>&1

# ════════════════════════════════════════════════════════════════
# 07 — LOGS (copied + redacted)
# ════════════════════════════════════════════════════════════════
if [ -f /var/log/vula/update.log ]; then
    tail -500 /var/log/vula/update.log | redact_stream > "$WORKDIR/logs/update.log"
fi
if [ -f /var/log/vula/apt-upgrade.log ]; then
    tail -200 /var/log/vula/apt-upgrade.log | redact_stream > "$WORKDIR/logs/apt-upgrade.log"
fi
if [ -f /var/log/vula/clamscan.log ]; then
    tail -50 /var/log/vula/clamscan.log | redact_stream > "$WORKDIR/logs/clamscan.log"
fi
if [ -f /var/log/vula/anydesk-handshake.log ]; then
    tail -50 /var/log/vula/anydesk-handshake.log | redact_stream > "$WORKDIR/logs/anydesk-handshake.log"
fi
if [ -n "$REAL_HOME" ] && [ -f "$REAL_HOME/.config/vula_print/logs/vula-print.log" ]; then
    tail -500 "$REAL_HOME/.config/vula_print/logs/vula-print.log" | \
        redact_stream > "$WORKDIR/logs/vula-print.log"
fi
for f in $(ls -t /var/log/vula-install-*.log 2>/dev/null | head -3); do
    cp "$f" "$WORKDIR/logs/$(basename "$f")"
    # Redact in-place (we just copied, safe to rewrite)
    redact_stream < "$f" > "$WORKDIR/logs/$(basename "$f")"
done

# ════════════════════════════════════════════════════════════════
# 08 — CONFIG SNAPSHOTS (redacted)
# ════════════════════════════════════════════════════════════════
cp -a /etc/vula/install.env "$WORKDIR/config/" 2>/dev/null || true
[ -f /etc/vula/creds.env ] && \
    grep -E '^[A-Z_]+=' /etc/vula/creds.env | \
        sed -E 's/^([A-Z_]+)=.+$/\1=<set>/;s/^([A-Z_]+)=$/\1=<empty>/' \
    > "$WORKDIR/config/creds.env.status"
[ -f /etc/vula/anydesk.json ] && \
    python3 -c "
import json
d = json.load(open('/etc/vula/anydesk.json'))
d['anydesk_password'] = '<REDACTED>' if d.get('anydesk_password') else None
print(json.dumps(d, indent=2))
" > "$WORKDIR/config/anydesk.json.redacted" 2>&1 || true
[ -n "$REAL_HOME" ] && [ -f "$REAL_HOME/.config/vula_print/settings.json" ] && \
    redact_stream < "$REAL_HOME/.config/vula_print/settings.json" \
    > "$WORKDIR/config/settings.json.redacted"

# ════════════════════════════════════════════════════════════════
# 09 — OPTIONAL: dmesg / lsusb
# ════════════════════════════════════════════════════════════════
if [ "$FULL" = "1" ]; then
    {
        emit "lsusb"
        lsusb 2>/dev/null || echo "(lsusb not installed)"
        echo
        emit "lsusb -t"
        lsusb -t 2>/dev/null || true
        echo
        emit "dmesg — USB"
        dmesg 2>/dev/null | grep -iE 'usb|printer|lp[0-9]|usblp' | tail -200
    } > "$WORKDIR/07-usb.txt" 2>&1
fi

# ════════════════════════════════════════════════════════════════
# 00 — SUMMARY (auto-flagged issues)
# ════════════════════════════════════════════════════════════════
{
    echo "═══════════════════════════════════════════════════════════════"
    echo "Vula! Print — audit summary"
    echo "Host: $HOST    Generated: $(date -Iseconds)    User: ${REAL_USER:-?}"
    echo "═══════════════════════════════════════════════════════════════"
    echo

    # Version
    V=$( [ -f "$SOURCE_DIR/VERSION" ] && cat "$SOURCE_DIR/VERSION" || echo "?" )
    echo "Version:          $V"

    # Service
    if [ -n "$REAL_USER" ]; then
        UID_N=$(id -u "$REAL_USER")
        SVC=$(sudo -u "$REAL_USER" \
            env XDG_RUNTIME_DIR="/run/user/$UID_N" \
                DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$UID_N/bus" \
            systemctl --user is-active vula-print.service 2>/dev/null || echo "unknown")
        echo "Print service:    $SVC"
    fi

    # Printers
    NP=$(ls /dev/usb/lp* 2>/dev/null | wc -l)
    NS=$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | wc -l)
    echo "Printers found:   $NP USB, $NS serial"
    if have lpstat; then
        NQ=$(lpstat -p 2>/dev/null | grep -c '^printer ' || echo 0)
        echo "CUPS queues:      $NQ"
    fi

    # Update state
    FAIL=$( [ -f /var/lib/vula/update-failure-count ] && cat /var/lib/vula/update-failure-count || echo 0 )
    echo "Update failures:  $FAIL consecutive"
    if [ "$FAIL" -ge 3 ]; then
        echo "  ⚠ ALERT — device is stuck; see logs/update.log"
    elif [ "$FAIL" -ge 1 ]; then
        echo "  ⚠ 1-2 recent failures — check logs/update.log"
    fi

    # Timers
    echo
    echo "Timers:"
    for t in vula-apt-upgrade vula-clamscan vula-print-update vula-anydesk-handshake; do
        if systemctl is-enabled --quiet "$t.timer" 2>/dev/null; then
            if systemctl is-active --quiet "$t.timer" 2>/dev/null; then
                echo "  ✓ $t.timer — enabled, active"
            else
                echo "  ! $t.timer — enabled, inactive"
            fi
        else
            echo "  ○ $t.timer — not enabled"
        fi
    done

    # Creds
    echo
    if [ -f /etc/vula/creds.env ]; then
        CRED_PERMS=$(stat -c '%A %U:%G' /etc/vula/creds.env)
        if [ "$(stat -c %a /etc/vula/creds.env)" = "600" ]; then
            echo "creds.env:        present, 0600 ✓"
        else
            echo "creds.env:        present but perms=$CRED_PERMS ⚠ expected 0600"
        fi
    else
        echo "creds.env:        ⚠ MISSING — N70 recovery will fire on next update"
    fi

    # Log perms
    if [ -f "$REAL_HOME/.config/vula_print/logs/vula-print.log" ]; then
        LOG_PERMS=$(stat -c %a "$REAL_HOME/.config/vula_print/logs/vula-print.log")
        if [ "$LOG_PERMS" = "600" ]; then
            echo "App log perms:    0600 ✓"
        else
            echo "App log perms:    $LOG_PERMS ⚠ expected 0600"
        fi
    fi

    # N72 (branch merge)
    echo
    if [ -n "$SOURCE_DIR" ] && [ -d "$SOURCE_DIR/.git" ]; then
        BRANCH=$(sudo -u "$REAL_USER" git -C "$SOURCE_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
        echo "Git branch:       $BRANCH"
        if [ "$BRANCH" = "main" ]; then
            HAS_MAIN=$(sudo -u "$REAL_USER" git -C "$SOURCE_DIR" rev-parse --verify origin/main 2>/dev/null && echo yes || echo no)
            if [ "$HAS_MAIN" = "no" ]; then
                echo "  ⚠ origin/main missing — update path may fail (N72)"
            fi
        fi
    fi

    # UI toggle
    if [ -f "$REAL_HOME/.config/vula_print/settings.json" ]; then
        LU=$(grep -oE '"log_upload_enabled"[[:space:]]*:[[:space:]]*(true|false)' \
             "$REAL_HOME/.config/vula_print/settings.json" 2>/dev/null | \
             grep -oE '(true|false)' || echo "true (default)")
        echo "Log upload (UI):  $LU"
    fi
    [ -f /etc/vula/log-upload-disabled ] && echo "Log upload (install): DISABLED by policy"

    echo
    echo "═══════════════════════════════════════════════════════════════"
    echo "Full detail in numbered files. All credentials redacted."
    echo "═══════════════════════════════════════════════════════════════"
} > "$WORKDIR/00-summary.txt" 2>&1

# ════════════════════════════════════════════════════════════════
# PACKAGE
# ════════════════════════════════════════════════════════════════
mkdir -p "$OUTPUT_DIR"
( cd "$WORKDIR" && tar czf "$TARBALL" . ) 2>/dev/null

if [ -f "$TARBALL" ]; then
    sha256sum "$TARBALL" > "${TARBALL}.sha256"
    echo
    echo "═══════════════════════════════════════════════════════════════"
    echo "Audit complete:  $TARBALL"
    echo "Checksum:        $(cat "${TARBALL}.sha256")"
    echo
    echo "Review summary:"
    echo "  tar -xzf $TARBALL -O ./00-summary.txt"
    echo
    echo "Send both $TARBALL and .sha256 back to the dev team."
    echo "═══════════════════════════════════════════════════════════════"
else
    echo "ERROR: failed to create tarball" >&2
    exit 1
fi