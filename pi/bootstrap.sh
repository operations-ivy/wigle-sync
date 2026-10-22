#!/usr/bin/env bash
# Builds a wardriving Pi from a fresh Raspberry Pi OS (or Kali) install: Kismet
# on wlan_mon0 and a USB Bluetooth dongle (hci0; the onboard Bluetooth is turned
# off) with gpsd, the home-wifi stop, the log size watch and the wigle-sync
# user. Idempotent; re-run after changing pi/.
#
#   scp -r pi zaphod@<pi>:/tmp/wigle-pi
#   ssh zaphod@<pi> 'sudo install -m 600 /tmp/wigle-pi/kismet-pi.env.example /etc/default/kismet-pi'
#   ssh -t zaphod@<pi> 'sudoedit /etc/default/kismet-pi'
#   ssh zaphod@<pi> 'sudo bash /tmp/wigle-pi/bootstrap.sh'
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SETTINGS=/etc/default/kismet-pi
KISMET_USER=zaphod
LOG_DIR=/var/log/kismet

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -f $SETTINGS ]] || { echo "$SETTINGS missing; start from $SRC/kismet-pi.env.example" >&2; exit 1; }
# shellcheck source=kismet-pi.env.example
. "$SETTINGS"
[[ -n ${HOME_WIFI_SSID:-} ]] || { echo "HOME_WIFI_SSID is empty in $SETTINGS" >&2; exit 1; }
chmod 600 "$SETTINGS"

# --- packages -----------------------------------------------------------------
. /etc/os-release
export DEBIAN_FRONTEND=noninteractive
if [[ $ID != kali ]]; then
    # Debian's kismet lags years behind; Kismet's own repo tracks releases.
    keyring=/usr/share/keyrings/kismet-archive-keyring.gpg
    if [[ ! -s $keyring ]]; then
        curl -fsSL https://www.kismetwireless.net/repos/kismet-release.gpg.key | gpg --dearmor -o "$keyring"
    fi
    echo "deb [signed-by=$keyring] https://www.kismetwireless.net/repos/apt/release/$VERSION_CODENAME $VERSION_CODENAME main" \
        > /etc/apt/sources.list.d/kismet.list
fi
# Kismet runs as $KISMET_USER, so its capture helpers must be privileged for the
# kismet group. Kali's packages ask kismet-capture-common/..., Kismet's repo kismet-common/...
debconf-set-selections <<'SEL'
kismet-capture-common kismet-capture-common/install-setuid boolean true
kismet-capture-common kismet-common/install-setuid boolean true
SEL
apt-get update -q
apt-get install -y -q kismet gpsd gpsd-clients iw acl
usermod -aG kismet "$KISMET_USER"
# gpsd adopts a USB GPS on its udev "add" event, so one plugged in before gpsd
# was installed is ignored until replugged. Replay the event.
udevadm trigger --action=add --subsystem-match=tty

# --- files --------------------------------------------------------------------
changed_links=0
while read -r rel; do
    dest="/${rel#./}"
    mode=644
    [[ -x "$SRC/rootfs/$rel" ]] && mode=755
    cmp -s "$SRC/rootfs/$rel" "$dest" && continue
    install -D -m "$mode" -o root -g root "$SRC/rootfs/$rel" "$dest"
    echo "installed $dest"
    # A changed .link only renames radios once they're re-plugged or the Pi reboots.
    [[ $dest == /etc/systemd/network/*.link ]] && changed_links=1
done < <(cd "$SRC/rootfs" && find . -type f | sort)

# --- the phone AP, retired 2026-10-15 ------------------------------------------
# Its RTL8188EUS dongle's USB slot went to a long-range Bluetooth dongle. Remove
# what earlier runs installed: the hostapd/dnsmasq units and config (the AP
# passphrase), the radio's name, and the patched DKMS driver and its blacklist.
for unit in kismet-ap.service kismet-ap-dnsmasq.service; do
    if [[ -e /etc/systemd/system/$unit ]]; then
        systemctl disable --now "$unit" 2>/dev/null || true
        rm -f "/etc/systemd/system/$unit"
        echo "removed $unit"
    fi
done
rm -rf /etc/kismet-ap /etc/hostapd/hostapd-wlan_ap.conf /etc/systemd/network/10-wlan_ap.link
if dpkg -s hostapd >/dev/null 2>&1; then apt-get purge -y -q hostapd; fi
# `dkms remove` wants module/version; read each version from `dkms status`.
if command -v dkms >/dev/null; then
    dkms status -m realtek-rtl8188eus 2>/dev/null | sed -n 's|^\(realtek-rtl8188eus/[^,:]*\).*|\1|p' | sort -u |
        while read -r mv; do
            dkms remove "$mv" --all && echo "removed $mv" || echo "dkms couldn't remove $mv; removing its files below"
        done
fi
# DKMS can't remove a module whose source is gone, so drop the built module too.
rm -rf /usr/src/realtek-rtl8188eus-* /var/lib/dkms/realtek-rtl8188eus /etc/modprobe.d/rtl8188eus.conf
if compgen -G "/lib/modules/*/updates/dkms/8188eu.ko*" >/dev/null; then
    rm -f /lib/modules/*/updates/dkms/8188eu.ko*
    depmod -a
    echo "removed the 8188eu module"
fi

install -d -m 2770 -o "$KISMET_USER" -g kismet "$LOG_DIR"

# The onboard Bluetooth is off (it shares a chip and antenna with wlan_host), so
# the USB dongle is always hci0. Takes effect at the next boot.
bt_reboot=0
config=/boot/firmware/config.txt
if [[ -f $config ]] && ! grep -qx "dtoverlay=disable-bt" "$config"; then
    printf '\n# wigle-sync: onboard Bluetooth off; a USB dongle is hci0 (pi/bootstrap.sh)\ndtoverlay=disable-bt\n' >> "$config"
    echo "onboard Bluetooth turned off in $config"
    bt_reboot=1
fi

# A Bluetooth adapter can come up soft-blocked; Kismet's hci0 source needs it
# unblocked. systemd-rfkill saves the state, so this survives reboots.
for r in /sys/class/rfkill/rfkill*; do
    [[ -e $r/type && $(<"$r/type") == bluetooth && $(<"$r/soft") == 1 ]] || continue
    echo 0 > "$r/soft"
    echo "unblocked $(<"$r/name")"
done

# --- services -----------------------------------------------------------------
udevadm control --reload
systemctl daemon-reload
nmcli general reload conf 2>/dev/null || systemctl reload NetworkManager
systemctl enable gpsd.socket wifi-regdomain.service kismet.service
systemctl start wifi-regdomain.service
# Kismet itself is left alone: starting it at home would record a home session.

bash "$SRC/setup-kismet-size-watch.sh"
bash "$SRC/metrics/setup-metrics.sh"
bash "$SRC/leds/setup-leds.sh"
if [[ -n ${WIGLE_SYNC_PUBKEY:-} ]]; then
    bash "$SRC/setup-pi-user.sh" "$WIGLE_SYNC_PUBKEY"
else
    echo "WIGLE_SYNC_PUBKEY empty; skipped the wigle-sync user"
fi

# --- checks -------------------------------------------------------------------
status=0
check() { if "${@:2}" >/dev/null 2>&1; then echo "ok:   $1"; else echo "FAIL: $1"; status=1; fi; }
check "wlan_mon0 exists" ip link show wlan_mon0
check "wlan_host exists" ip link show wlan_host
phy_supports() { local phy; phy=$(iw dev "$1" info | awk '/wiphy/ {print "phy"$2}'); iw phy "$phy" info | sed -n '/Supported interface modes/,/^\t[^\t]/p' | grep -qx "[[:space:]]*\* $2"; }
check "wlan_mon0 supports monitor mode" phy_supports wlan_mon0 monitor
check "kismet_site.conf captures from wlan_mon0" grep -qx "source=wlan_mon0" /etc/kismet/kismet_site.conf
check "kismet_site.conf captures from hci0" grep -q "^source=hci0" /etc/kismet/kismet_site.conf
check "hci0 exists" test -e /sys/class/bluetooth/hci0
# The dongle, not the onboard chip (hci_uart_bcm): needs disable-bt and a reboot.
check "hci0 is a USB adapter" sh -c 'grep -qx "DRIVER=btusb" /sys/class/bluetooth/hci0/device/uevent'
bt_unblocked() { local r; for r in /sys/class/rfkill/rfkill*; do [[ $(<"$r/type") == bluetooth && $(<"$r/soft") == 1 ]] && return 1; done; return 0; }
check "bluetooth not blocked" bt_unblocked
# Kismet's packages grant file capabilities when setcap exists, else setuid root.
helper_privileged() { test -u "$1" || /usr/sbin/getcap "$1" | grep -q cap_net_raw; }
check "kismet capture helper is privileged" helper_privileged /usr/bin/kismet_cap_linux_wifi
check "kismet bluetooth helper is privileged" helper_privileged /usr/bin/kismet_cap_linux_bluetooth
check "node-exporter answers on localhost" sh -c 'curl -fsS http://127.0.0.1:9100/metrics | grep -q "^pi_gps_fix_mode"'
check "Prometheus agent running" systemctl is-active prometheus.service
check "chrony running" systemctl is-active chrony.service
check "gpsd sees a device" sh -c 'timeout 10 gpspipe -w -n 5 2>/dev/null | grep -q "\"class\":\"DEVICES\".*\"path\""'
(( changed_links )) && echo "note: interface names changed; reboot to apply them"
(( bt_reboot )) && echo "note: reboot to turn the onboard Bluetooth off (the dongle becomes hci0)"
exit $status
