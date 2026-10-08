#!/usr/bin/env bash
# Builds a wardriving Pi from a fresh Raspberry Pi OS (or Kali) install: Kismet
# on wlan_mon0 with gpsd, the phone AP on wlan_ap, the home-wifi stop, the log
# size watch and the wigle-sync user. Idempotent; re-run after changing pi/.
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
for var in HOME_WIFI_SSID AP_SSID AP_PASSPHRASE AP_CHANNEL; do
    [[ -n ${!var:-} ]] || { echo "$var is empty in $SETTINGS" >&2; exit 1; }
done
(( ${#AP_PASSPHRASE} >= 8 && ${#AP_PASSPHRASE} <= 63 )) || { echo "AP_PASSPHRASE must be 8-63 characters" >&2; exit 1; }
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
# Kismet runs as $KISMET_USER, so its capture helpers must be setuid for the kismet group.
echo "kismet-capture-common kismet-capture-common/install-setuid boolean true" | debconf-set-selections
apt-get update -q
# dnsmasq-base, not dnsmasq: only kismet-ap-dnsmasq.service should run it.
apt-get install -y -q kismet gpsd gpsd-clients hostapd dnsmasq-base iw acl
usermod -aG kismet "$KISMET_USER"

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

hostapd_conf=/etc/hostapd/hostapd-wlan_ap.conf
new_conf=$(cat <<CONF
interface=wlan_ap
driver=nl80211
ssid=$AP_SSID
country_code=US
hw_mode=g
channel=$AP_CHANNEL
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=$AP_PASSPHRASE
CONF
)
ap_changed=0
if [[ "$(cat "$hostapd_conf" 2>/dev/null)" != "$new_conf" ]]; then
    install -d -m 755 /etc/hostapd
    (umask 077; printf '%s\n' "$new_conf" > "$hostapd_conf")
    echo "installed $hostapd_conf"
    ap_changed=1
fi

install -d -m 2770 -o "$KISMET_USER" -g kismet "$LOG_DIR"

# --- services -----------------------------------------------------------------
udevadm control --reload
systemctl daemon-reload
nmcli general reload conf 2>/dev/null || systemctl reload NetworkManager
systemctl enable gpsd.socket wifi-regdomain.service kismet.service kismet-ap.service kismet-ap-dnsmasq.service
systemctl start wifi-regdomain.service
if ip link show wlan_ap >/dev/null 2>&1; then
    if (( ap_changed )); then systemctl restart kismet-ap.service; else systemctl start kismet-ap.service; fi
fi
# Kismet itself is left alone: starting it at home would record a home session.

bash "$SRC/setup-kismet-size-watch.sh"
if [[ -n ${WIGLE_SYNC_PUBKEY:-} ]]; then
    bash "$SRC/setup-pi-user.sh" "$WIGLE_SYNC_PUBKEY"
else
    echo "WIGLE_SYNC_PUBKEY empty; skipped the wigle-sync user"
fi

# --- checks -------------------------------------------------------------------
status=0
check() { if "${@:2}" >/dev/null 2>&1; then echo "ok:   $1"; else echo "FAIL: $1"; status=1; fi; }
check "wlan_mon0 exists" ip link show wlan_mon0
check "wlan_ap exists" ip link show wlan_ap
check "wlan_host exists" ip link show wlan_host
phy_supports() { local phy; phy=$(iw dev "$1" info | awk '/wiphy/ {print "phy"$2}'); iw phy "$phy" info | sed -n '/Supported interface modes/,/^\t[^\t]/p' | grep -qx "[[:space:]]*\* $2"; }
check "wlan_mon0 supports monitor mode" phy_supports wlan_mon0 monitor
check "wlan_ap supports AP mode" phy_supports wlan_ap AP
check "kismet_site.conf captures from wlan_mon0" grep -qx "source=wlan_mon0" /etc/kismet/kismet_site.conf
check "kismet capture helper is setuid" test -u /usr/bin/kismet_cap_linux_wifi
check "kismet-ap (hostapd) running" systemctl is-active kismet-ap.service
check "gpsd sees a device" sh -c 'gpspipe -w -n 5 2>/dev/null | grep -q "\"class\":\"DEVICES\".*\"path\""'
(( changed_links )) && echo "note: interface names changed; reboot to apply them"
exit $status
