#!/usr/bin/env bash
# Kismet's web login (Kismet runs as $KISMET_USER, bootstrap.sh). Until one is
# set, whoever reaches port 2501 first gets to choose it; this sets a random
# one, kept only here. pi-metrics, status-leds and eink-status read it from
# this path (LoadCredential). Idempotent; run by bootstrap.sh and
# leds/setup-leds.sh.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
command -v openssl >/dev/null || apt-get install -y -q --no-install-recommends openssl

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
