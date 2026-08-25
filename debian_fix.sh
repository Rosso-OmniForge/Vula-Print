#!/bin/bash
# ============================================================
#  Debian 13 (Trixie) Post-Install Bootstrap  — v3
#  Covers: firmware, drivers (CPU/GPU/NIC/BT/audio),
#          system hardening, desktop readiness, and more.
# ============================================================
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[*]${NC} $*"; }
ok()      { echo -e "${GREEN}[+]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
err()     { echo -e "${RED}[-]${NC} $*"; }
section() { echo -e "\n${BOLD}${CYAN}══ $* ══${NC}"; }

# ── Root check ───────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    err "Run this as root: su - then bash $0"
    exit 1
fi

# ── Log everything ───────────────────────────────────────────
LOGFILE="/var/log/debian-bootstrap.log"
exec > >(tee -a "$LOGFILE") 2>&1
info "Full log: $LOGFILE"

# ── Validate target user ─────────────────────────────────────
while true; do
    read -rp "$(echo -e "${CYAN}[?]${NC} Standard username to add to sudo: ")" TARGET_USER
    TARGET_USER=$(echo "$TARGET_USER" | xargs)
    if id "$TARGET_USER" &>/dev/null; then
        ok "User '$TARGET_USER' found."
        break
    else
        err "User '$TARGET_USER' does not exist. Try again."
    fi
done

# ── Hardware detection ───────────────────────────────────────
section "Hardware Detection"

CPU_VENDOR=$(grep -m1 'vendor_id' /proc/cpuinfo | awk '{print $3}' || true)
GPU_INFO=$(lspci 2>/dev/null | grep -Ei 'vga|3d|display' || true)
HAS_NVIDIA=$(echo "$GPU_INFO" | grep -i nvidia  || true)
HAS_AMD_GPU=$(echo "$GPU_INFO" | grep -i 'amd\|ati\|radeon' || true)
HAS_INTEL_GPU=$(echo "$GPU_INFO" | grep -i intel || true)

NIC_INFO=$(lspci 2>/dev/null | grep -i 'network\|ethernet\|wireless\|wi-fi\|wifi' || true)
HAS_IWLWIFI=$(echo "$NIC_INFO" | grep -i intel || true)
HAS_REALTEK=$(echo "$NIC_INFO" | grep -i realtek || true)
HAS_ATHEROS=$(echo "$NIC_INFO" | grep -i 'atheros\|qualcomm' || true)
HAS_BROADCOM=$(echo "$NIC_INFO" | grep -i broadcom || true)
HAS_MEDIATEK=$(echo "$NIC_INFO" | grep -i 'mediatek\|mt76' || true)

HAS_BT=$(lsusb 2>/dev/null | grep -i bluetooth || lspci 2>/dev/null | grep -i bluetooth || true)
IS_LAPTOP=$(cat /sys/class/dmi/id/chassis_type 2>/dev/null || echo "0")
IS_SSD=$(lsblk -d -o NAME,ROTA 2>/dev/null | awk '$2=="0"{found=1} END{print found+0}')
IS_VM=$(systemd-detect-virt 2>/dev/null || true)

info "CPU vendor  : ${CPU_VENDOR:-unknown}"
info "GPU(s)      : ${GPU_INFO:-none detected}"
info "NICs        : ${NIC_INFO:-none detected}"
info "Bluetooth   : ${HAS_BT:-none detected}"
info "Virtual env : ${IS_VM:-none}"
info "SSD present : $( [ "$IS_SSD" = "1" ] && echo yes || echo no )"
info "Chassis type: $IS_LAPTOP"

# ── APT sources ──────────────────────────────────────────────
section "APT Sources"
info "Rebuilding /etc/apt/sources.list for Trixie…"
cat > /etc/apt/sources.list <<'EOF'
# ── Debian 13 Trixie ──────────────────────────────────────────
deb     http://deb.debian.org/debian/           trixie           main contrib non-free non-free-firmware
deb-src http://deb.debian.org/debian/           trixie           main contrib non-free non-free-firmware

deb     http://security.debian.org/debian-security  trixie-security  main contrib non-free non-free-firmware
deb-src http://security.debian.org/debian-security  trixie-security  main contrib non-free non-free-firmware

deb     http://deb.debian.org/debian/           trixie-updates   main contrib non-free non-free-firmware
deb-src http://deb.debian.org/debian/           trixie-updates   main contrib non-free non-free-firmware
EOF
ok "sources.list written."

info "Updating package index and upgrading system…"
apt-get update -qq
apt-get full-upgrade -y
ok "System up to date."

# ── Sudo ─────────────────────────────────────────────────────
section "Sudo"
apt-get install -y sudo
usermod -aG sudo "$TARGET_USER"
# Ensure /etc/sudoers.d/ is included (it usually is, but just in case)
grep -q '#includedir /etc/sudoers.d' /etc/sudoers || \
    echo '#includedir /etc/sudoers.d' >> /etc/sudoers
ok "User '$TARGET_USER' added to group sudo."

# ── CPU microcode ────────────────────────────────────────────
section "CPU Microcode"
if echo "$CPU_VENDOR" | grep -q "GenuineIntel"; then
    apt-get install -y intel-microcode
    ok "Intel microcode installed."
elif echo "$CPU_VENDOR" | grep -q "AuthenticAMD"; then
    apt-get install -y amd64-microcode
    ok "AMD microcode installed."
else
    warn "Unknown CPU vendor — skipping microcode."
fi

# ── Core firmware (always) ───────────────────────────────────
section "Core Firmware"
apt-get install -y \
    firmware-linux \
    firmware-linux-free \
    firmware-linux-nonfree \
    firmware-misc-nonfree
ok "Core firmware installed."

# ── NIC / Wireless firmware ──────────────────────────────────
section "Network Adapter Firmware"
NIC_PKGS=()
[ -n "$HAS_IWLWIFI"  ] && NIC_PKGS+=(firmware-iwlwifi)   && info "Intel WiFi  firmware queued."
[ -n "$HAS_REALTEK"  ] && NIC_PKGS+=(firmware-realtek)   && info "Realtek     firmware queued."
[ -n "$HAS_ATHEROS"  ] && NIC_PKGS+=(firmware-atheros)   && info "Atheros     firmware queued."
[ -n "$HAS_BROADCOM" ] && NIC_PKGS+=(firmware-brcm80211) && info "Broadcom    firmware queued."
[ -n "$HAS_MEDIATEK" ] && NIC_PKGS+=(firmware-mediatek)  && info "MediaTek    firmware queued."

if [ ${#NIC_PKGS[@]} -gt 0 ]; then
    apt-get install -y "${NIC_PKGS[@]}"
    ok "NIC firmware installed: ${NIC_PKGS[*]}"
else
    warn "No specific NIC firmware detected — core firmware should suffice."
fi

# ── GPU drivers ──────────────────────────────────────────────
section "GPU Drivers"

if [ -n "$HAS_NVIDIA" ] && [ "$IS_VM" = "none" ]; then
    info "NVIDIA GPU detected. Installing proprietary driver…"
    # Enable contrib+non-free is already in sources; add apt pinning for nvidia
    apt-get install -y \
        linux-headers-amd64 \
        nvidia-driver \
        nvidia-settings \
        nvidia-smi
    ok "NVIDIA driver installed."
    warn "A reboot is required before the NVIDIA driver activates."

elif [ -n "$HAS_AMD_GPU" ]; then
    info "AMD/Radeon GPU detected. Installing Mesa + firmware…"
    apt-get install -y \
        mesa-vulkan-drivers \
        mesa-va-drivers \
        mesa-vdpau-drivers \
        libgl1-mesa-dri \
        firmware-amd-graphics \
        radeontop
    ok "AMD/Mesa drivers installed."

elif [ -n "$HAS_INTEL_GPU" ]; then
    info "Intel GPU detected. Installing Mesa + VA-API…"
    apt-get install -y \
        mesa-vulkan-drivers \
        mesa-va-drivers \
        i965-va-driver \
        intel-media-va-driver \
        libgl1-mesa-dri \
        intel-gpu-tools
    ok "Intel GPU drivers installed."

else
    warn "No discrete GPU matched — only mesa generic packages installed."
    apt-get install -y libgl1-mesa-dri mesa-utils
fi

# ── Audio ─────────────────────────────────────────────────────
section "Audio (PipeWire)"
apt-get install -y \
    pipewire \
    pipewire-audio \
    pipewire-pulse \
    pipewire-alsa \
    wireplumber \
    alsa-utils \
    pavucontrol

# Enable PipeWire for the target user (systemd user services)
sudo -u "$TARGET_USER" XDG_RUNTIME_DIR="/run/user/$(id -u "$TARGET_USER")" \
    systemctl --user enable pipewire pipewire-pulse wireplumber 2>/dev/null || \
    warn "Could not enable PipeWire user services now — will activate on first login."
ok "PipeWire audio stack installed."

# ── Bluetooth ─────────────────────────────────────────────────
section "Bluetooth"
if [ -n "$HAS_BT" ]; then
    apt-get install -y \
        bluez \
        bluetooth \
        blueman \
        firmware-bluetooth
    systemctl enable bluetooth
    ok "Bluetooth stack installed and service enabled."
else
    info "No Bluetooth hardware detected — skipping."
fi

# ── Networking tools ─────────────────────────────────────────
section "Networking"
apt-get install -y \
    network-manager \
    network-manager-gnome \
    wpasupplicant \
    wireless-tools \
    rfkill \
    iw \
    net-tools \
    curl \
    wget \
    dnsutils \
    traceroute \
    nmap \
    openssh-client

systemctl enable NetworkManager
ok "NetworkManager enabled."

# ── Build tools & kernel headers ─────────────────────────────
section "Build Tools & Kernel Headers"
apt-get install -y \
    build-essential \
    dkms \
    linux-headers-amd64 \
    make \
    cmake \
    gcc \
    g++ \
    pkg-config \
    git \
    git-lfs \
    ca-certificates \
    gnupg \
    lsb-release \
    apt-transport-https \
    software-properties-common
ok "Build essentials and kernel headers installed."

# ── Firewall ─────────────────────────────────────────────────
section "Firewall (UFW)"
apt-get install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
# Do NOT enable automatically — user must review rules first
ok "UFW installed with sensible defaults. Enable with: ufw enable"

# ── AppArmor ─────────────────────────────────────────────────
section "AppArmor (MAC)"
apt-get install -y apparmor apparmor-utils apparmor-profiles apparmor-profiles-extra
systemctl enable apparmor
# Ensure GRUB passes the kernel flag
if [ -f /etc/default/grub ]; then
    if ! grep -q 'apparmor=1' /etc/default/grub; then
        sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="\(.*\)"/GRUB_CMDLINE_LINUX_DEFAULT="\1 apparmor=1 security=apparmor"/' /etc/default/grub
        update-grub 2>/dev/null || true
        info "AppArmor added to GRUB kernel parameters."
    fi
fi
ok "AppArmor enabled."

# ── SSD TRIM ─────────────────────────────────────────────────
section "SSD TRIM"
if [ "$IS_SSD" = "1" ]; then
    systemctl enable fstrim.timer
    ok "Weekly TRIM timer enabled (fstrim.timer)."
else
    info "No SSD detected — skipping TRIM."
fi

# ── Power management ─────────────────────────────────────────
section "Power Management"
# Chassis types 8-10 = Notebook/Laptop/Sub-Notebook
if echo "$IS_LAPTOP" | grep -qE '^(8|9|10)$'; then
    info "Laptop detected. Installing TLP…"
    apt-get install -y tlp tlp-rdw powertop
    systemctl enable tlp
    ok "TLP power management installed and enabled."
else
    info "Desktop/VM detected — skipping TLP."
fi

# Intel thermal daemon
if echo "$CPU_VENDOR" | grep -q "GenuineIntel" && [ "$IS_VM" = "none" ]; then
    apt-get install -y thermald
    systemctl enable thermald
    ok "thermald installed (Intel CPU throttling daemon)."
fi

# ── Printing ─────────────────────────────────────────────────
section "Printing (CUPS)"
apt-get install -y \
    cups \
    cups-client \
    printer-driver-all \
    system-config-printer
systemctl enable cups
usermod -aG lpadmin "$TARGET_USER"
ok "CUPS printer stack installed. Web UI: http://localhost:631"

# ── Flatpak ──────────────────────────────────────────────────
section "Flatpak"
apt-get install -y flatpak
if command -v gnome-software &>/dev/null; then
    apt-get install -y gnome-software-plugin-flatpak
fi
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || \
    warn "Could not reach Flathub (no internet?). Add manually later."
ok "Flatpak + Flathub configured."

# ── Virtualisation support ───────────────────────────────────
section "Virtualisation Support"
if [ "$IS_VM" = "none" ]; then
    # KVM / QEMU for running VMs
    apt-get install -y \
        qemu-kvm \
        libvirt-daemon-system \
        libvirt-clients \
        virt-manager \
        bridge-utils
    usermod -aG libvirt,kvm "$TARGET_USER"
    systemctl enable libvirtd
    ok "KVM/QEMU virtualisation stack installed."
else
    info "Running inside ${IS_VM} — skipping KVM install."
    # Install guest additions / tools if relevant
    case "$IS_VM" in
        vmware)
            apt-get install -y open-vm-tools open-vm-tools-desktop
            ok "VMware guest tools installed."
            ;;
        oracle)
            apt-get install -y virtualbox-guest-utils virtualbox-guest-x11 2>/dev/null || \
                warn "VirtualBox guest additions not found in repo — install manually."
            ;;
        kvm|qemu)
            apt-get install -y qemu-guest-agent spice-vdagent
            systemctl enable qemu-guest-agent
            ok "QEMU guest agent installed."
            ;;
    esac
fi

# ── Locale & timezone ────────────────────────────────────────
section "Locale & Timezone"
if ! locale -a 2>/dev/null | grep -q 'en_US.utf8'; then
    info "Generating en_US.UTF-8 locale…"
    sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen
    locale-gen
    update-locale LANG=en_US.UTF-8
    ok "Locale set to en_US.UTF-8."
fi
info "Current timezone: $(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone)"
warn "Set timezone with: timedatectl set-timezone <Region/City>"

# ── System utilities ─────────────────────────────────────────
section "System Utilities"
apt-get install -y \
    bash-completion \
    command-not-found \
    htop \
    btop \
    ncdu \
    tree \
    unzip \
    zip \
    p7zip-full \
    rsync \
    lsof \
    strace \
    man-db \
    less \
    vim \
    nano \
    tmux \
    screen \
    smartmontools \
    lm-sensors \
    hdparm \
    pciutils \
    usbutils \
    dmidecode \
    inxi \
    fastfetch \
    xdg-utils \
    xdg-user-dirs
xdg-user-dirs-update 2>/dev/null || true
ok "System utilities installed."

# ── Sensors init ─────────────────────────────────────────────
section "Hardware Sensors"
if [ "$IS_VM" = "none" ]; then
    yes "" | sensors-detect --auto >/dev/null 2>&1 || true
    ok "Sensors auto-detected. Use: sensors"
fi

# ── GRUB timeout tweak (optional quality-of-life) ────────────
section "GRUB"
if [ -f /etc/default/grub ]; then
    # Only change timeout if it's the Debian default (5s)
    sed -i 's/^GRUB_TIMEOUT=5$/GRUB_TIMEOUT=3/' /etc/default/grub
    update-grub 2>/dev/null || true
    ok "GRUB updated."
fi

# ── Final system snapshot ────────────────────────────────────
section "System Snapshot"
info "Kernel    : $(uname -r)"
info "Packages  : $(dpkg -l | grep -c '^ii') installed"
info "Disk use  : $(df -h / | awk 'NR==2{print $3"/"$2" ("$5" used)"}')"
inxi -Fxz --no-host 2>/dev/null || true

# ── Summary ──────────────────────────────────────────────────
section "Bootstrap Complete"
echo ""
echo -e "${BOLD}Action items before first use:${NC}"
echo -e "  ${YELLOW}1.${NC} Enable firewall        : ${CYAN}ufw enable${NC}"
echo -e "  ${YELLOW}2.${NC} Set your timezone       : ${CYAN}timedatectl set-timezone Region/City${NC}"
echo -e "  ${YELLOW}3.${NC} Sudo takes effect after : ${CYAN}logout/login or newgrp sudo${NC}"
if [ -n "$HAS_NVIDIA" ] && [ "$IS_VM" = "none" ]; then
echo -e "  ${YELLOW}4.${NC} NVIDIA driver           : ${CYAN}reboot required to activate${NC}"
fi
echo -e "  ${YELLOW}5.${NC} Review CUPS printers    : ${CYAN}http://localhost:631${NC}"
echo -e "  ${YELLOW}6.${NC} Flatpak apps available  : ${CYAN}flatpak install flathub <app-id>${NC}"
echo -e "  ${YELLOW}7.${NC} Full log saved to       : ${CYAN}$LOGFILE${NC}"
echo ""
ok "Reboot strongly recommended: ${CYAN}systemctl reboot${NC}"s