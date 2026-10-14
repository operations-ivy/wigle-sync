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
# - A Prometheus agent that scrapes it every 30 s and remote-writes to the
#   cluster's Prometheus; away from home it keeps the samples in its WAL (up
#   to a day) and sends them once it's back.
# - chrony instead of systemd-timesyncd, with gpsd's GPS time as a fallback
#   source, so samples (and Kismet's logs) taken away from the internet carry
#   the right time. gpsd runs with -n so it reads the GPS with no client.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

apt-get install -y -q --no-install-recommends prometheus-node-exporter prometheus chrony python3
systemctl disable --now systemd-timesyncd.service 2>/dev/null || true

# Time: GPS as a fallback source.
install -D -m 644 "$SRC/chrony-gps.conf" /etc/chrony/conf.d/gps.conf
if ! grep -q '^GPSD_OPTIONS=.*-n' /etc/default/gpsd; then
    sed -i 's/^GPSD_OPTIONS="\(.*\)"/GPSD_OPTIONS="-n \1"/; s/-n "/-n"/' /etc/default/gpsd
    systemctl restart gpsd.service 2>/dev/null || true
fi
systemctl enable chrony.service
systemctl restart chrony.service

# node-exporter: localhost only, textfile directory on tmpfs.
echo 'd /run/node-exporter-textfile 0755 root root -' > /etc/tmpfiles.d/node-exporter-textfile.conf
systemd-tmpfiles --create /etc/tmpfiles.d/node-exporter-textfile.conf
node_args='ARGS="--web.listen-address=127.0.0.1:9100 --collector.textfile.directory=/run/node-exporter-textfile --collector.systemd --collector.systemd.enable-restarts-metrics --collector.systemd.unit-include=.+[.](service|timer)"'
if [[ "$(cat /etc/default/prometheus-node-exporter 2>/dev/null)" != "$node_args" ]]; then
    printf '%s\n' "$node_args" > /etc/default/prometheus-node-exporter
    systemctl restart prometheus-node-exporter.service
fi
systemctl enable --now prometheus-node-exporter.service

install -m 755 "$SRC/pi-metrics.py" /usr/local/sbin/pi-metrics
install -m 644 "$SRC/pi-metrics.service" "$SRC/pi-metrics.timer" /etc/systemd/system/

# The Prometheus agent. Debian's package (2.53) runs a full server; ARGS turns
# it into an agent with its own config and WAL. The server it started on
# install leaves an unused database in metrics2/.
install -D -m 644 "$SRC/agent.yml" /etc/prometheus/agent.yml
agent_args='ARGS="--enable-feature=agent --config.file=/etc/prometheus/agent.yml --storage.agent.path=/var/lib/prometheus/agent --storage.agent.retention.max-time=24h --web.listen-address=127.0.0.1:9090"'
if [[ "$(cat /etc/default/prometheus 2>/dev/null)" != "$agent_args" ]]; then
    printf '%s\n' "$agent_args" > /etc/default/prometheus
    systemctl restart prometheus.service
fi
rm -rf /var/lib/prometheus/metrics2

systemctl daemon-reload
systemctl enable --now prometheus.service pi-metrics.timer
systemctl start pi-metrics.service  # the first readings now, not in 30 s
echo "metrics: node-exporter, pi-metrics and the Prometheus agent running"
