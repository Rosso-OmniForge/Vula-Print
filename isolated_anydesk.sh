#!/usr/bin/env bash
# ============================================================
#  VULA! ANYDESK DOWNLOADER
#  Friendly guided installer for Debian 13 (Trixie) + KDE Plasma
#
#  What it does, in order:
#    1. Downloads & installs AnyDesk
#    2. If the session is on Wayland, switches SDDM to X11
#    3. Runs apt update && apt upgrade -y
#    4. Asks if the user wants the Printer App
#         N -> asks them to reboot (so X11 takes effect)
#         Y -> walks them through getting a Printer API key,
#              then runs ./install.sh with sudo
# ============================================================

set -e

# ---------- colours ----------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'   # no colour

# ---------- small helpers ----------
pause_enter() {
    read -rp "$(echo -e "${BLUE}  Press Enter to continue...${NC}")"
}

ask_yes_no() {
    # $1 = question text, returns 0 for yes, 1 for no
    local answer
    while true; do
        read -rp "$(echo -e "${YELLOW}${1} [y/n]: ${NC}")" answer
        case "$answer" in
            [Yy]|[Yy][Ee][Ss]) return 0 ;;
            [Nn]|[Nn][Oo]) return 1 ;;
            *) echo -e "${RED}  Please type y or n.${NC}" ;;
        esac
    done
}

step_header() {
    echo ""
    echo -e "${CYAN}${BOLD}=========================================================${NC}"
    echo -e "${CYAN}${BOLD}  $1${NC}"
    echo -e "${CYAN}${BOLD}=========================================================${NC}"
}

# ---------- banner ----------
clear
echo -e "${CYAN}${BOLD}"
cat << "EOF"
 __      ____  _      _    _
 \ \    / /  \| |    / \  | |
  \ \  / /| | |   | /   \ | |
   \ \/ / | |_| |  / /_\ \ |_|
    \  /  |  _  | / _____ \ _
     \/   |_| |_|/_/     \_(_)

     V U L A !   A N Y D E S K   D O W N L O A D E R
EOF
echo -e "${NC}"
echo -e "${BLUE}Hi! This assistant will set up AnyDesk remote support on your computer.${NC}"
echo -e "${BLUE}Don't worry if you're new to Linux — just read each step and answer${NC}"
echo -e "${BLUE}the questions when asked. We'll go one step at a time.${NC}"
pause_enter

# ---------- sanity checks ----------
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[!] This script needs admin rights to install software.${NC}"
    echo -e "${RED}    Please run it again like this:${NC}"
    echo -e "${YELLOW}      sudo bash $0${NC}"
    exit 1
fi

if ! ping -c 1 -W 1 8.8.8.8 &>/dev/null; then
    echo -e "${RED}[!] No internet connection detected.${NC}"
    echo -e "${RED}    Please connect to Wi-Fi or a network cable, then run this again.${NC}"
    exit 1
fi

# ---------- work out who the real logged-in desktop user is ----------
# We're running as root (via sudo), but we need to know which actual
# human user is sitting at the screen so we can (a) correctly detect
# Wayland vs X11, and (b) launch GUI apps as them, not as root.
REAL_USER="${SUDO_USER:-}"

if [[ -z "$REAL_USER" ]]; then
    # Fallback: ask loginctl who is logged in on a graphical seat
    REAL_USER="$(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $3}' | head -n1)"
fi

REAL_SESSION_TYPE="unknown"
if [[ -n "$REAL_USER" ]]; then
    REAL_SESSION_ID="$(loginctl list-sessions --no-legend 2>/dev/null | awk -v u="$REAL_USER" '$3==u {print $1; exit}')"
    if [[ -n "$REAL_SESSION_ID" ]]; then
        REAL_SESSION_TYPE="$(loginctl show-session "$REAL_SESSION_ID" -p Type --value 2>/dev/null || echo unknown)"
    fi
fi

# Figure out the real user's DISPLAY and XAUTHORITY so we can launch
# GUI apps as them (instead of hitting "Authorization required" as root).
REAL_USER_UID=""
REAL_DISPLAY=""
REAL_XAUTHORITY=""
if [[ -n "$REAL_USER" ]]; then
    REAL_USER_UID="$(id -u "$REAL_USER" 2>/dev/null || echo "")"
    REAL_DISPLAY="$(loginctl show-session "$REAL_SESSION_ID" -p Display --value 2>/dev/null || echo ":0")"
    [[ -z "$REAL_DISPLAY" ]] && REAL_DISPLAY=":0"
    if [[ -n "$REAL_USER_UID" ]]; then
        # Common XAUTHORITY locations on Debian/KDE
        if [[ -f "/run/user/${REAL_USER_UID}/gdm/Xauthority" ]]; then
            REAL_XAUTHORITY="/run/user/${REAL_USER_UID}/gdm/Xauthority"
        elif [[ -f "/home/${REAL_USER}/.Xauthority" ]]; then
            REAL_XAUTHORITY="/home/${REAL_USER}/.Xauthority"
        fi
    fi
fi

# Helper: run a GUI command as the real logged-in user, with their
# display + auth, so it doesn't fail with "Authorization required".
run_as_real_user_gui() {
    if [[ -n "$REAL_USER" && -n "$REAL_USER_UID" ]]; then
        sudo -u "$REAL_USER" \
            DISPLAY="$REAL_DISPLAY" \
            XAUTHORITY="$REAL_XAUTHORITY" \
            XDG_RUNTIME_DIR="/run/user/${REAL_USER_UID}" \
            "$@"
    else
        # Fall back to running directly (best effort) if we couldn't
        # work out who the real user is.
        "$@"
    fi
}

# ============================================================
# STEP 1 — Download & install AnyDesk
# ============================================================
step_header "STEP 1 of 4 — Installing AnyDesk"
echo -e "${BLUE}AnyDesk lets our support team connect to your screen to help you${NC}"
echo -e "${BLUE}whenever you need assistance. This is the easy part — just sit tight.${NC}"
echo ""

URL="https://download.anydesk.com/linux/anydesk_8.1.0-1_amd64.deb"
DEB="anydesk_8.1.0-1_amd64.deb"

echo -e "${YELLOW}Downloading AnyDesk...${NC}"
if [[ -f "$DEB" ]]; then
    echo -e "${GREEN}  Already downloaded — skipping.${NC}"
else
    wget -q --show-progress "$URL" -O "$DEB"
    echo -e "${GREEN}  Done.${NC}"
fi

echo -e "${YELLOW}Installing AnyDesk...${NC}"
dpkg -i "$DEB" || apt-get install -f -y
echo -e "${GREEN}  AnyDesk is installed.${NC}"

echo -e "${YELLOW}Setting AnyDesk to start automatically...${NC}"
systemctl daemon-reload
systemctl enable --now anydesk.service
echo -e "${GREEN}  Done. AnyDesk will now run quietly in the background every time you turn on your computer.${NC}"

echo ""
echo -e "${BLUE}A small AnyDesk window may now open on your screen.${NC}"
echo -e "${BLUE}If it does, please click 'Accept' on the license agreement.${NC}"
if [[ -n "$REAL_USER" ]]; then
    run_as_real_user_gui anydesk &>/dev/null &
else
    echo -e "${YELLOW}  (Skipping auto-open — couldn't detect your desktop session.${NC}"
    echo -e "${YELLOW}   You can open AnyDesk yourself from the applications menu later.)${NC}"
fi
sleep 5

echo -e "${GREEN}${BOLD}Step 1 complete! AnyDesk is installed.${NC}"
pause_enter

# ============================================================
# STEP 2 — Switch Wayland to X11 (KDE Plasma / SDDM only)
# ============================================================
step_header "STEP 2 of 4 — Checking your display setup"
echo -e "${BLUE}Your computer can run its desktop in one of two modes: 'Wayland' or 'X11'.${NC}"
echo -e "${BLUE}AnyDesk needs 'X11' mode to be able to see and share your screen properly.${NC}"
echo -e "${BLUE}We'll check which one you're using now.${NC}"
echo ""

CURRENT_SESSION="$REAL_SESSION_TYPE"
NEEDS_REBOOT_FOR_X11=0

echo -e "${BLUE}(Detected session type: ${CURRENT_SESSION})${NC}"
echo ""

if [[ "$CURRENT_SESSION" == "wayland" ]]; then
    echo -e "${YELLOW}You are currently using Wayland. We need to switch this to X11.${NC}"

    SDDM_DIR="/etc/sddm.conf.d"
    SDDM_FILE="$SDDM_DIR/10-vula-x11.conf"
    mkdir -p "$SDDM_DIR"

    cat > "$SDDM_FILE" << 'SDDMEOF'
[General]
# Added by VULA! ANYDESK DOWNLOADER to force the X11 session
DisplayServer=x11

[Wayland]
EnableHiDPI=false
SDDMEOF

    echo -e "${GREEN}  Done. Your computer is now set to use X11 after you restart.${NC}"
    NEEDS_REBOOT_FOR_X11=1
elif [[ "$CURRENT_SESSION" == "x11" ]]; then
    echo -e "${GREEN}  Good news — you're already set up correctly on X11. No changes needed.${NC}"
else
    echo -e "${YELLOW}  We couldn't automatically confirm your session type.${NC}"
    echo -e "${YELLOW}  To be safe, we'll set X11 as the default anyway — it won't cause any harm${NC}"
    echo -e "${YELLOW}  even if you're already using it.${NC}"

    SDDM_DIR="/etc/sddm.conf.d"
    SDDM_FILE="$SDDM_DIR/10-vula-x11.conf"
    mkdir -p "$SDDM_DIR"

    cat > "$SDDM_FILE" << 'SDDMEOF'
[General]
# Added by VULA! ANYDESK DOWNLOADER to force the X11 session
DisplayServer=x11

[Wayland]
EnableHiDPI=false
SDDMEOF
    echo -e "${GREEN}  Done.${NC}"
    NEEDS_REBOOT_FOR_X11=1
fi

echo -e "${GREEN}${BOLD}Step 2 complete!${NC}"
pause_enter

# ============================================================
# STEP 3 — Update the system
# ============================================================
step_header "STEP 3 of 4 — Updating your system"
echo -e "${BLUE}We'll now check for and install any important updates.${NC}"
echo -e "${BLUE}This can take a few minutes — that's completely normal. Please be patient.${NC}"
echo ""

apt update && apt upgrade -y

echo ""
echo -e "${GREEN}${BOLD}Step 3 complete! Your system is up to date.${NC}"
pause_enter

# ============================================================
# STEP 4 — Printer App (optional) / reboot
# ============================================================
step_header "STEP 4 of 4 — Printer App"
echo -e "${BLUE}Next, we can optionally set up the Printer App. This lets you print${NC}"
echo -e "${BLUE}directly to your store's printers. It's a great next step once you're${NC}"
echo -e "${BLUE}comfortable using AnyDesk.${NC}"
echo ""

if ask_yes_no "Do you want to install the Printer app now?"; then
    echo ""
    echo -e "${CYAN}${BOLD}Great! Let's get your Printer API key first.${NC}"
    echo ""
    echo -e "${BLUE}Please open a web browser and go to this address:${NC}"
    echo -e "${YELLOW}${BOLD}  https://store.bedoonessm.co.za/admin/settings?tab=printers${NC}"
    echo ""
    echo -e "${BLUE}Once there:${NC}"
    echo -e "${BLUE}  1. Click 'Add New Printer'${NC}"
    echo -e "${BLUE}  2. Fill in the details for your printer${NC}"
    echo -e "${BLUE}  3. Copy the API Key that is shown to you — you'll need it in a moment${NC}"
    echo ""
    echo -e "${BLUE}(Tip: keep this terminal window open and switch back to it once you have the key.)${NC}"
    echo ""

    # Loop until they confirm they actually have the key
    while ! ask_yes_no "Have you got your API key yet?"; do
        echo -e "${YELLOW}No problem — take your time. Go back to the browser and copy the API key.${NC}"
    done

    echo ""
    echo -e "${GREEN}Perfect! Now we'll install the Printer App.${NC}"
    echo -e "${BLUE}The installer will ask you for the store web address and the API key you just copied.${NC}"
    echo -e "${BLUE}The web address is: ${YELLOW}https://store.bedoonessm.co.za${NC}"
    echo -e "${BLUE}(You can also add another store later by clicking 'Manage' once this is installed.)${NC}"
    echo ""

    echo -e "${BLUE}Installing the Printer App needs admin rights. Please type your login${NC}"
    echo -e "${BLUE}password below (the same one you use to log into this computer).${NC}"
    echo -e "${BLUE}Note: nothing will appear on screen as you type — that's normal, just type and press Enter.${NC}"
    echo ""

    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    INSTALL_SH="$SCRIPT_DIR/install.sh"

    if [[ ! -f "$INSTALL_SH" ]]; then
        echo -e "${RED}[!] Couldn't find install.sh in: $SCRIPT_DIR${NC}"
        echo -e "${RED}    Please make sure install.sh is in the same folder as this script, then try again.${NC}"
        exit 1
    fi

    chmod +x "$INSTALL_SH"

    # If we're already root (script was run with sudo), just run it directly.
    # Otherwise prompt properly via sudo -v so the password prompt is clean and friendly.
    if [[ $EUID -ne 0 ]]; then
        sudo -v
    fi

    sudo "$INSTALL_SH"

    echo ""
    echo -e "${GREEN}${BOLD}The Printer App has been installed.${NC}"
else
    echo ""
    echo -e "${BLUE}No problem — you can always install the Printer App later by running${NC}"
    echo -e "${BLUE}this script again.${NC}"
fi

# ---------- final message ----------
echo ""
echo -e "${GREEN}${BOLD}"
cat << "EOF"
 =========================================
   VULA! SETUP COMPLETE
 =========================================
EOF
echo -e "${NC}"

if [[ "$NEEDS_REBOOT_FOR_X11" -eq 1 ]]; then
    echo -e "${YELLOW}${BOLD}One last thing: please restart your computer now.${NC}"
    echo -e "${BLUE}This finishes switching your screen to X11 mode so AnyDesk works properly.${NC}"
    echo ""
    read -rp "$(echo -e "${YELLOW}Press Enter to restart now, or Ctrl+C to restart later...${NC}")"
    reboot
else
    echo -e "${GREEN}You're all set! AnyDesk is installed and ready to go.${NC}"
fi