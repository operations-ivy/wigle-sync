#!/usr/bin/env bash
# The phone AP (README, "The phone AP"). Run by bootstrap.sh, after Kismet's
# login exists (kismet-ap-switch.service loads it); idempotent.
#
# KISMET_AP_SSID in /etc/default/kismet-pi turns it on (the user, 2026-11-21):
# NetworkManager's kismet-ap profile on wlan_host, started and stopped by
# kismet-ap-switch, so Kismet's web UI is on the phone on the road. The AP's
# address goes in own-radios.txt in Kismet's log dir, which wigle-sync reads to
# leave the AP out of uploads: it travels with the car. Empty: all of it goes.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
SETTINGS=/etc/default/kismet-pi
KISMET_USER=${KISMET_USER:-zaphod}
LOG_DIR=${LOG_DIR:-/var/log/kismet}
# shellcheck source=../kismet-pi.env.example
. "$SETTINGS"

install -m 755 "$SRC/kismet-ap-switch" /usr/local/sbin/kismet-ap-switch
install -m 644 "$SRC/kismet-ap-switch.service" /etc/systemd/system/kismet-ap-switch.service
systemctl daemon-reload

# KISMET_AP_SSID turns it on (the user, 2026-11-21): NetworkManager's kismet-ap
# profile, started and stopped by kismet-ap-switch, so Kismet's web UI is on the
# phone on the road. Its address goes in own-radios.txt, which wigle-sync reads
# to leave the AP out of uploads: it travels with the car.
own_radios=$LOG_DIR/own-radios.txt
if [[ -n ${KISMET_AP_SSID:-} ]]; then
    (( ${#KISMET_AP_PSK} >= 8 && ${#KISMET_AP_PSK} <= 63 )) || { echo "KISMET_AP_PSK is 8 to 63 characters" >&2; exit 1; }
    ap_settings=(connection.interface-name wlan_host connection.autoconnect no
        802-11-wireless.mode ap 802-11-wireless.ssid "$KISMET_AP_SSID"
        802-11-wireless.band a 802-11-wireless.channel "${KISMET_AP_CHANNEL:-149}"
        802-11-wireless.cloned-mac-address permanent
        wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp
        wifi-sec.psk "$KISMET_AP_PSK"
        ipv4.method shared ipv4.addresses "${KISMET_AP_ADDRESS:-192.168.50.1/24}" ipv6.method disabled)
    if nmcli -t connection show kismet-ap >/dev/null 2>&1; then
        nmcli connection modify kismet-ap "${ap_settings[@]}"
    else
        nmcli connection add type wifi con-name kismet-ap "${ap_settings[@]}" >/dev/null
        echo "added the kismet-ap profile"
    fi
    want="$(printf '%s\n' "# The Pi's own radios: wigle-sync leaves them out of uploads (pi/bootstrap.sh)." \
        "$(</sys/class/net/wlan_host/address)")"
    if [[ ! -f $own_radios || $(<"$own_radios") != "$want" ]]; then
        printf '%s\n' "$want" > "$own_radios"
        echo "wrote $own_radios"
    fi
    chown "$KISMET_USER:kismet" "$own_radios"
    chmod 640 "$own_radios"
    systemctl enable kismet-ap-switch.service
    systemctl restart kismet-ap-switch.service
    echo "ap: kismet-ap-switch running"
else
    if nmcli -t connection show kismet-ap >/dev/null 2>&1; then
        nmcli connection down kismet-ap 2>/dev/null || true
        nmcli connection delete kismet-ap >/dev/null
        echo "removed the kismet-ap profile"
    fi
    systemctl disable --now kismet-ap-switch.service 2>/dev/null || true
    rm -f "$own_radios"
fi

