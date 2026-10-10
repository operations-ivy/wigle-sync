#!/usr/bin/env bash
# Gives the RTL8188EUS (wlan_ap) AP mode. The in-kernel rtl8xxxu driver only does
# managed and monitor for this chip, so hostapd fails with "Could not configure
# driver mode". Builds aircrack-ng/rtl8188eus through DKMS, patched for current
# kernels by rtl8188eus-kernel-compat.py, and blacklists rtl8xxxu. DKMS rebuilds
# it on kernel upgrades. Idempotent; bootstrap.sh runs it.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
REPO=https://github.com/aircrack-ng/rtl8188eus.git
COMMIT=af3bf004458f76b7aec33e9ba552cd382ed1f5c3
NAME=realtek-rtl8188eus
VERSION="5.3.9~20221105"   # PACKAGE_VERSION in the driver's dkms.conf
DEST="/usr/src/$NAME-$VERSION"
KVER="$(uname -r)"

ap_capable() {
    local phy
    phy=$(cat /sys/class/net/wlan_ap/phy80211/name 2>/dev/null) || return 1
    iw phy "$phy" info | sed -n '/Supported interface modes/,/^\t[^\t]/p' | grep -qx "[[:space:]]*\* AP"
}

if ap_capable; then
    echo "wlan_ap already supports AP mode; skipped the rtl8188eus driver"
    exit 0
fi

if ! dkms status -m "$NAME" -v "$VERSION" -k "$KVER" 2>/dev/null | grep -q installed; then
    apt-get install -y -q dkms git build-essential bc "linux-headers-$KVER"
    if [[ "$(git -c safe.directory='*' -C "$DEST" rev-parse HEAD 2>/dev/null)" != "$COMMIT" ]]; then
        rm -rf "$DEST"
        git clone -q "$REPO" "$DEST"
        git -c safe.directory='*' -C "$DEST" checkout -q "$COMMIT"
    fi
    python3 -I "$SRC/rtl8188eus-kernel-compat.py" "$DEST"
    dkms add -m "$NAME" -v "$VERSION" 2>/dev/null || true
    dkms build -m "$NAME" -v "$VERSION" -k "$KVER"
    dkms install -m "$NAME" -v "$VERSION" -k "$KVER"
fi

echo "blacklist rtl8xxxu" > /etc/modprobe.d/rtl8188eus.conf
if lsmod | grep -q '^rtl8xxxu '; then
    modprobe -r rtl8xxxu
fi
modprobe 8188eu
echo "rtl8188eus driver installed"
