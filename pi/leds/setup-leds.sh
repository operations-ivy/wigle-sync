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
# - LED2 (middle) alternates red and white near a Flock camera or Axon device,
#   and is off otherwise.
# - LED3 (bottom) shows the SoC's temperature: green, yellow, orange, flashing
#   red. Its red and green mix on the kernel's software PWM (pwm-gpio overlays
#   on GPIO17 and 27); next boot.
# - LED2 and LED3 are status-leds.py, asking Kismet's REST API with a login
#   only this Pi knows. Their other pins are held low from boot so the default
#   pull-up on GPIO5 doesn't light LED2 faintly before it starts.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

apt-get install -y -q --no-install-recommends python3 python3-libgpiod python3-structlog openssl

config=/boot/firmware/config.txt
marker="# wigle-sync: RGB status LEDs (pi/leds/setup-leds.sh)"
led_lines=(
    "dtoverlay=gpio-led,gpio=16,label=led1-red,trigger=actpwr"
    "dtoverlay=gpio-led,gpio=20,label=led1-green,trigger=mmc0"
    "dtoverlay=pwm-gpio,gpio=17"
    "dtoverlay=pwm-gpio,gpio=27"
    "gpio=5,6,13,21,22=op,dl"
)
# Lines earlier versions of this script added.
old_lines=(
    "gpio=5,13,17,21,22,27=op,dl"
    "gpio=5,13,21,27=op,dl"
    "dtoverlay=pwm-gpio,gpio=6"
    "dtoverlay=pwm-gpio,gpio=22"
)
if [[ -f $config ]]; then
    # The old lines go with their comments (tidy-config.awk).
    for line in "${old_lines[@]}"; do
        grep -qxF "$line" "$config" || continue
        echo "removing from $config: $line"
    done
    tidy=$(OLD="$(printf '%s\n' "${old_lines[@]}")" awk -v MARK="$marker" -f "$SRC/tidy-config.awk" "$config")
    if [[ $tidy != "$(cat "$config")" ]]; then
        printf '%s\n' "$tidy" > "$config"
        echo "tidied $config"
    fi
    for line in "${led_lines[@]}"; do
        grep -qxF "$line" "$config" && continue
        printf '%s\n%s\n' "$marker" "$line" >> "$config"
        echo "added to $config: $line"
        echo "note: reboot for the status LEDs"
    done
fi

# Kismet's web login (Kismet runs as $KISMET_USER, bootstrap.sh). Until one is
# set, whoever reaches port 2501 first gets to choose it; this sets a random
# one, kept only here. status-leds.service reads it from this path.
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

# Renamed: led2-alert (LED2 only) became status-leds.
if [[ -e /etc/systemd/system/led2-alert.service ]]; then
    systemctl disable --now led2-alert.service
    rm -f /etc/systemd/system/led2-alert.service /usr/local/lib/wigle-leds/led2-alert.py
fi

install -d -m 755 /usr/local/lib/wigle-leds
install -m 644 "$SRC/status-leds.py" "$SRC/detect.py" /usr/local/lib/wigle-leds/
install -m 644 "$SRC/status-leds.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable status-leds.service
systemctl restart status-leds.service
echo "leds: LED1 copies the onboard LEDs, status-leds running (LED2, LED3)"
