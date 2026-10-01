#!/usr/bin/env bash
# Installs the Kismet log size watcher (a oneshot service run every 5 minutes by a timer).
# Idempotent. Copy pi/ to the Pi and run it there as root:
#
#   scp pi/kismet-size-watch.* pi/setup-kismet-size-watch.sh zaphod@<pi>:/tmp/
#   ssh zaphod@<pi> 'sudo bash /tmp/setup-kismet-size-watch.sh'
set -euo pipefail

SRC="$(dirname "$0")"

install -m 755 "$SRC/kismet-size-watch.sh" /usr/local/sbin/kismet-size-watch
install -m 644 "$SRC/kismet-size-watch.service" "$SRC/kismet-size-watch.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now kismet-size-watch.timer
echo "kismet-size-watch timer enabled"
