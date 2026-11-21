#!/usr/bin/env bash
# The e-ink status display (README, "A Pi Zero 2 W handheld"). Run by
# bootstrap.sh when /etc/default/kismet-pi has EINK=1; idempotent.
#
# - SPI on (the panel's bus). Next boot.
# - Waveshare's Python driver for the 2.13" panels (waveshare_epd: the
#   SPI/GPIO glue and the V2, V3 and V4 drivers), fetched from Waveshare's
#   e-Paper repo at a pinned commit and checked by SHA-256. It's not packaged
#   for Debian; pwnagotchi vendors the same files.
# - eink-status.service, drawing with Pillow: DotGothic16 (a pixel font) for
#   the text, DejaVu for the face, Terminus for its label.
#
# The panel's reset pin is GPIO17, LED3's red on brick69's RGB status LEDs,
# so the two can't share a Pi (setup-zero2w.sh skips the LEDs).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

COMMIT=06e834491bf62023a1b86a481b4530978883d2c4  # waveshareteam/e-Paper, 2026-07-28
BASE="https://raw.githubusercontent.com/waveshareteam/e-Paper/$COMMIT/RaspberryPi_JetsonNano/python/lib/waveshare_epd"
declare -A SHA256=(
    [epdconfig.py]=e53e361f5061b3a62ca143001e20afc530593035556addd57ecd4dbf815ec9f3
    [epd2in13_V2.py]=52fdcb8f713f0fcfbc56dd67e6d6fce075dfd6ecd2ae1d2dd8240b4f41c4f6fe
    [epd2in13_V3.py]=12dd331cdc032095aa60fb3aeb8666876e4713324c1e7a78af10b51e878ca7f2
    [epd2in13_V4.py]=9f5df7236029898f06426dfd61b3a4361c3cfafad3835d1db28ffcd377a3a00c
)
LIB=/usr/local/lib/wigle-eink

apt-get install -y -q --no-install-recommends python3 python3-pil python3-spidev python3-gpiozero python3-lgpio \
    python3-structlog python3-yaml fonts-dejavu-core fonts-terminus-otb fonts-dotgothic16 curl

config=/boot/firmware/config.txt
if [[ -f $config ]] && ! grep -qx 'dtparam=spi=on' "$config"; then
    printf '%s\n%s\n' "# wigle-sync: SPI for the e-ink display (pi/eink/setup-eink.sh)" "dtparam=spi=on" >> "$config"
    echo "SPI on in $config (reboot)"
fi

install -d -m 755 /etc/wigle-pi "$LIB/waveshare_epd"
[[ -e /etc/wigle-pi/handheld.yaml ]] || install -m 644 "$SRC/../handheld.example.yaml" /etc/wigle-pi/handheld.yaml

# The driver: each file fetched only when missing or changed, and checked.
for f in "${!SHA256[@]}"; do
    dest="$LIB/waveshare_epd/$f"
    if [[ -f $dest ]] && echo "${SHA256[$f]}  $dest" | sha256sum -c --quiet 2>/dev/null; then
        continue
    fi
    tmp=$(mktemp)
    curl -fsSL -o "$tmp" "$BASE/$f"
    echo "${SHA256[$f]}  $tmp" | sha256sum -c --quiet
    install -m 644 "$tmp" "$dest"
    rm -f "$tmp"
    echo "installed waveshare_epd/$f"
done
: > "$LIB/waveshare_epd/__init__.py"

install -m 644 "$SRC/eink-status.py" "$SRC/eink_art.py" "$SRC/../handheld_common.py" "$LIB/"
install -m 644 "$SRC/eink-status.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable eink-status.service
if [[ -e /dev/spidev0.0 ]]; then
    systemctl restart eink-status.service
    echo "eink: eink-status running"
else
    echo "eink: eink-status starts after a reboot (SPI)"
fi
