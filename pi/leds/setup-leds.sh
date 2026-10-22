#!/usr/bin/env bash
# The RGB status LEDs on the Pi's header (README, "Status LEDs"). Idempotent;
# run by bootstrap.sh, or alone:
#
#   scp -r pi zaphod@<pi>:/tmp/wigle-pi
#   ssh zaphod@<pi> 'sudo bash /tmp/wigle-pi/leds/setup-leds.sh'
#
# - LED1 (top) copies the onboard LEDs the case hides: red on (actpwr: power,
#   off during activity), green on SD activity (mmc0, like ACT). Kernel
#   gpio-led overlays in config.txt; next boot.
# - LED2 (middle) flashes red and white near a Flock camera or Axon device,
#   and breathes green otherwise: led2-alert.py, asking Kismet's REST API with
#   a login only this Pi knows. Green fades on the kernel's software PWM
#   (pwm-gpio overlay on GPIO6); next boot.
# - LED3 (bottom) has no job yet. LED2's and LED3's pins are held low from boot
#   so their default pull-ups (GPIO 5) don't light them faintly.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

apt-get install -y -q --no-install-recommends python3 python3-libgpiod openssl

config=/boot/firmware/config.txt
led_lines=(
    "dtoverlay=gpio-led,gpio=16,label=led1-red,trigger=actpwr"
    "dtoverlay=gpio-led,gpio=20,label=led1-green,trigger=mmc0"
    "dtoverlay=pwm-gpio,gpio=6"
    "gpio=5,13,17,21,22,27=op,dl"
)
if [[ -f $config ]]; then
    for line in "${led_lines[@]}"; do
        grep -qxF "$line" "$config" && continue
        printf '# wigle-sync: RGB status LEDs (pi/leds/setup-leds.sh)\n%s\n' "$line" >> "$config"
        echo "added to $config: $line"
        echo "note: reboot for the status LEDs"
    done
fi

# Kismet's web login (Kismet runs as $KISMET_USER, bootstrap.sh). Until one is
# set, whoever reaches port 2501 first gets to choose it; this sets a random
# one, kept only here. led2-alert.service reads it from this path.
KISMET_USER=${KISMET_USER:-zaphod}
httpd="$(getent passwd "$KISMET_USER" | cut -d: -f6)/.kismet/kismet_httpd.conf"
if ! grep -q '^httpd_password=.' "$httpd" 2>/dev/null; then
    install -d -m 700 -o "$KISMET_USER" -g "$KISMET_USER" "$(dirname "$httpd")"
    ( umask 077; printf 'httpd_username=wigle\nhttpd_password=%s\n' "$(openssl rand -hex 16)" > "$httpd" )
    chown "$KISMET_USER:$KISMET_USER" "$httpd"
    echo "set Kismet's web login in $httpd"
    if systemctl is-active -q kismet.service; then
        systemctl restart kismet.service
    fi
fi

install -d -m 755 /usr/local/lib/wigle-leds
install -m 644 "$SRC/led2-alert.py" "$SRC/detect.py" /usr/local/lib/wigle-leds/
install -m 644 "$SRC/led2-alert.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable led2-alert.service
systemctl restart led2-alert.service
echo "leds: LED1 copies the onboard LEDs, led2-alert running"
