#!/usr/bin/env bash
# The PiSugar 2 battery under a Pi Zero 2 W (README, "A Pi Zero 2 W
# handheld"). Run by bootstrap.sh when /etc/default/kismet-pi has PISUGAR=1;
# idempotent.
#
# - pisugar-server, PiSugar's own daemon (it owns the board's I2C chips: the
#   IP5209 power chip at 0x75 and the SD3078 RTC at 0x32), from the project's
#   GitHub release, pinned and checked by SHA-256. It listens on its Unix
#   socket only: the package's defaults also open a web UI and an
#   unauthenticated TCP API on every interface, which a wardriver on strange
#   networks must not have.
# - pisugar-poweroff, from the same release: at the end of a shutdown it
#   tells the PiSugar to cut its output after a countdown, so a halted Pi
#   (and its USB dongles) doesn't drain the battery flat. Ordered after
#   umount.target here (pisugar-poweroff-order.conf), so that's once
#   everything else has stopped.
# - pisugar-watch (pisugar-watch.py): the battery as metrics, a clean
#   shutdown when it's low, and the RTC as the clock's fallback.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

VERSION=2.3.5
declare -A SHA256=(
    [pisugar-server_${VERSION}-1_arm64.deb]=e28e7bcee544b13858094b610f53a9870099543b8652c079a7eb462598248873
    [pisugar-poweroff_${VERSION}-1_arm64.deb]=c5d2c625b371f98b474d5dc6ef0ef414454dfc26afa489490ceef41b67745097
)
POWEROFF_COUNTDOWN=10  # seconds from pisugar-poweroff's start (after umount.target) to the power cut
# arm64 only: the handheld runs 64-bit Raspberry Pi OS, as brick69 does.

apt-get install -y -q --no-install-recommends python3 python3-structlog python3-yaml debconf-utils i2c-tools curl

install -d -m 755 /etc/wigle-pi
[[ -e /etc/wigle-pi/handheld.yaml ]] || install -m 644 "$SRC/../handheld.example.yaml" /etc/wigle-pi/handheld.yaml
model=$(python3 -c 'import sys, yaml; c = yaml.safe_load(open(sys.argv[1])) or {}; print((c.get("battery") or {}).get("model") or "PiSugar 2 (4-LEDs)")' /etc/wigle-pi/handheld.yaml)
case $model in
    "PiSugar 2 (4-LEDs)" | "PiSugar 2 (2-LEDs)") ;;
    *) echo "battery model '$model' in /etc/wigle-pi/handheld.yaml isn't a PiSugar 2" >&2; exit 1 ;;
esac

# I2C on the header's pins 3 and 5, where the PiSugar's pogo pins touch. Next boot.
config=/boot/firmware/config.txt
if [[ -f $config ]] && ! grep -qx 'dtparam=i2c_arm=on' "$config"; then
    printf '%s\n%s\n' "# wigle-sync: I2C for the PiSugar (pi/pisugar/setup-pisugar.sh)" "dtparam=i2c_arm=on" >> "$config"
    echo "I2C on in $config (reboot)"
fi
grep -qx i2c-dev /etc/modules 2>/dev/null || echo i2c-dev >> /etc/modules
modprobe i2c-dev 2>/dev/null || true

# --- pisugar-server and pisugar-poweroff -------------------------------------
arch=$(dpkg --print-architecture)
debconf-set-selections <<SEL
pisugar-server pisugar-server/model select $model
pisugar-server pisugar-server/address select 127.0.0.1
pisugar-server pisugar-server/web boolean false
pisugar-server pisugar-server/tcp boolean false
pisugar-server pisugar-server/uds boolean true
pisugar-poweroff pisugar-poweroff/model select $model
pisugar-poweroff pisugar-poweroff/countdown select $POWEROFF_COUNTDOWN
SEL
for pkg in pisugar-server pisugar-poweroff; do
    deb="${pkg}_${VERSION}-1_${arch}.deb"
    [[ -n ${SHA256[$deb]:-} ]] || { echo "no pinned $deb for $arch" >&2; exit 1; }
    if [[ $(dpkg-query -W -f '${Version}' "$pkg" 2>/dev/null) == "${VERSION}-1" ]]; then
        continue
    fi
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/$deb" "https://github.com/PiSugar/pisugar-power-manager-rs/releases/download/v${VERSION}/$deb"
    echo "${SHA256[$deb]}  $tmp/$deb" | sha256sum -c --quiet
    dpkg -i "$tmp/$deb"
    rm -rf "$tmp"
    echo "installed $pkg $VERSION"
done
# Written here as well as by the packages' debconf, so a re-run puts them back.
server_opts="OPTS=\"--model '$model' --syslog --config config.json --uds /tmp/pisugar-server.sock\""
if [[ "$(cat /etc/default/pisugar-server 2>/dev/null)" != "$server_opts" ]]; then
    printf '%s\n' "$server_opts" > /etc/default/pisugar-server
    systemctl restart pisugar-server.service 2>/dev/null || true
fi
printf '%s\n' "OPTS=\"--model '$model' --countdown $POWEROFF_COUNTDOWN\"" > /etc/default/pisugar-poweroff

# --- pisugar-watch -----------------------------------------------------------
install -m 644 "$SRC/wigle-pi-state.conf" /etc/tmpfiles.d/wigle-pi-state.conf
systemd-tmpfiles --create /etc/tmpfiles.d/wigle-pi-state.conf
install -d -m 755 /usr/local/lib/wigle-pisugar
install -m 644 "$SRC/pisugar-watch.py" "$SRC/../handheld_common.py" /usr/local/lib/wigle-pisugar/
install -m 644 "$SRC/pisugar-watch.service" "$SRC/pisugar-rtc-load.service" /etc/systemd/system/
install -D -m 644 "$SRC/pisugar-poweroff-order.conf" /etc/systemd/system/pisugar-poweroff.service.d/order.conf
systemctl daemon-reload
systemctl enable pisugar-server.service pisugar-poweroff.service pisugar-watch.service pisugar-rtc-load.service
systemctl restart pisugar-server.service pisugar-watch.service
echo "pisugar: pisugar-server on its socket only, pisugar-watch running ($model)"
