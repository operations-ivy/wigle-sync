#!/usr/bin/env bash
# Metrics for the wardriving Pi (README, "Metrics"). Idempotent; run by
# bootstrap.sh, or alone:
#
#   scp -r pi zaphod@<pi>:/tmp/wigle-pi
#   ssh zaphod@<pi> 'sudo bash /tmp/wigle-pi/metrics/setup-metrics.sh'
#
# - node-exporter on 127.0.0.1:9100 only (nothing on the phone's AP or a
#   strange network can reach it), with the systemd collector and a textfile
#   directory on tmpfs for pi-metrics.py's readings.
# - vmagent (Debian's victoria-metrics package), which scrapes it every 30 s
#   and remote-writes to the cluster's Prometheus; away from home it keeps
#   the samples in an on-disk queue and sends them once it's back, even
#   after the reboot in between (#47: the Prometheus agent this replaced
#   resends nothing from before a restart).
# - chrony instead of systemd-timesyncd, with gpsd's GPS time as a fallback
#   source, so samples (and Kismet's logs) taken away from the internet carry
#   the right time. gpsd runs with -n so it reads the GPS with no client.
# - fake-hwclock: there's no RTC, so a boot starts on the time it saved at
#   shutdown (and hourly) rather than whenever systemd last noted it.
#   wait-for-clock.service (rootfs) then holds Kismet back, up to 3 minutes,
#   until chrony has the real time.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

apt-get install -y -q --no-install-recommends prometheus-node-exporter victoria-metrics chrony fake-hwclock python3
systemctl disable --now systemd-timesyncd.service 2>/dev/null || true

# Time: GPS as a fallback source.
install -D -m 644 "$SRC/chrony-gps.conf" /etc/chrony/conf.d/gps.conf
if ! grep -q '^GPSD_OPTIONS=.*-n' /etc/default/gpsd; then
    sed -i 's/^GPSD_OPTIONS="\(.*\)"/GPSD_OPTIONS="-n \1"/; s/-n "/-n"/' /etc/default/gpsd
    systemctl restart gpsd.service 2>/dev/null || true
fi
systemctl enable chrony.service fake-hwclock-load.service fake-hwclock-save.timer
systemctl restart chrony.service
fake-hwclock save

# node-exporter: localhost only, textfile directory on tmpfs.
echo 'd /run/node-exporter-textfile 0755 root root -' > /etc/tmpfiles.d/node-exporter-textfile.conf
systemd-tmpfiles --create /etc/tmpfiles.d/node-exporter-textfile.conf
node_args='ARGS="--web.listen-address=127.0.0.1:9100 --collector.textfile.directory=/run/node-exporter-textfile --collector.systemd --collector.systemd.enable-restarts-metrics --collector.systemd.unit-include=.+[.](service|timer) --collector.wifi"'
if [[ "$(cat /etc/default/prometheus-node-exporter 2>/dev/null)" != "$node_args" ]]; then
    printf '%s\n' "$node_args" > /etc/default/prometheus-node-exporter
    systemctl restart prometheus-node-exporter.service
fi
systemctl enable --now prometheus-node-exporter.service

install -m 755 "$SRC/pi-metrics.py" /usr/local/sbin/pi-metrics
install -m 644 "$SRC/pi-metrics.service" "$SRC/pi-metrics.timer" /etc/systemd/system/

# vmagent, from the victoria-metrics package. The package's own service is a
# whole time-series database: never wanted here.
systemctl disable --now victoria-metrics.service 2>/dev/null || true
systemctl mask victoria-metrics.service
install -D -m 644 "$SRC/agent.yml" /etc/vmagent/agent.yml
install -m 644 "$SRC/vmagent.service" /etc/systemd/system/vmagent.service

# The Prometheus agent it replaced (#47), and that agent's WAL: what's in it
# can't be sent any more.
if dpkg -s prometheus >/dev/null 2>&1; then
    systemctl disable --now prometheus.service 2>/dev/null || true
    apt-get purge -y -q prometheus
fi
rm -rf /var/lib/prometheus /etc/prometheus/agent.yml /etc/default/prometheus

systemctl daemon-reload
systemctl enable --now vmagent.service pi-metrics.timer
systemctl reload-or-restart vmagent.service  # a changed agent.yml or unit
systemctl start pi-metrics.service  # the first readings now, not in 30 s
echo "metrics: node-exporter, pi-metrics and vmagent running"
