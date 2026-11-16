#!/usr/bin/env bash
# The Pi Zero 2 W profile (README, "A Pi Zero 2 W handheld"): fits the
# wardriving host into 512 MB. Run by bootstrap.sh when pi-profile says
# zero2w; does nothing on another board. Idempotent.
#
# - Swap in zram (compressed RAM), never a swap file on the SD card: rpi-swap
#   (Raspberry Pi OS trixie) set to zram only, or else dphys-swapfile
#   (bookworm) removed and systemd-zram-generator used.
# - Headless: no KMS graphics driver and the smallest GPU split, so its
#   memory goes to Linux. Next boot.
# - Kismet's device tracker capped (kismet_lowmem.conf), and the OOM killer
#   told to take the metrics before Kismet.
# - The RGB status LEDs aren't built: the e-ink display uses their pins.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

# --- swap: zram only ---------------------------------------------------------
if [[ -e /etc/rpi/swap.conf ]]; then
    install -D -m 644 "$SRC/90-zero2w-swap.conf" /etc/rpi/swap.conf.d/90-zero2w-swap.conf
    echo "rpi-swap: zram only (next boot)"
else
    if dpkg -s dphys-swapfile >/dev/null 2>&1; then
        dphys-swapfile swapoff || true
        apt-get purge -y -q dphys-swapfile
        rm -f /var/swap
        echo "removed dphys-swapfile and its swap file"
    fi
    apt-get install -y -q --no-install-recommends systemd-zram-generator
    install -D -m 644 "$SRC/zram-generator.conf" /etc/systemd/zram-generator.conf
    systemctl daemon-reload
    systemctl start systemd-zram-setup@zram0.service 2>/dev/null || echo "zram: starts at the next boot"
fi
install -m 644 "$SRC/90-zero2w-sysctl.conf" /etc/sysctl.d/90-zero2w.conf
sysctl -q -p /etc/sysctl.d/90-zero2w.conf

# --- config.txt: headless ----------------------------------------------------
config=/boot/firmware/config.txt
marker="# wigle-sync: Pi Zero 2 W, headless (pi/zero2w/setup-zero2w.sh)"
if [[ -f $config ]]; then
    if grep -q '^dtoverlay=vc4-kms-v3d' "$config"; then
        sed -i 's/^dtoverlay=vc4-kms-v3d/#&  # off: headless (wigle-sync zero2w)/' "$config"
        echo "turned the KMS graphics driver off in $config (next boot)"
    fi
    if ! grep -qx 'gpu_mem=16' "$config"; then
        printf '%s\ngpu_mem=16\n' "$marker" >> "$config"
        echo "added gpu_mem=16 to $config (next boot)"
    fi
fi

# --- Kismet and the Go services ---------------------------------------------
install -m 644 "$SRC/kismet_lowmem.conf" /etc/kismet/kismet_lowmem.conf
install -D -m 644 "$SRC/kismet-zero2w.conf" /etc/systemd/system/kismet.service.d/zero2w.conf
for unit in vmagent.service prometheus-node-exporter.service; do
    install -D -m 644 "$SRC/go-memlimit.conf" "/etc/systemd/system/$unit.d/zero2w.conf"
done
systemctl daemon-reload
# Running services pick the limits up at their next start; Kismet is left
# alone, as bootstrap.sh does (starting it at home records a home session).
systemctl try-restart vmagent.service prometheus-node-exporter.service
echo "zero2w: zram swap, headless, Kismet's tracker capped"
