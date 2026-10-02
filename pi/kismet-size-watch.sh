#!/usr/bin/env bash
# Restarts Kismet once the .kismet log it is writing passes KISMET_MAX_MB, so each
# session stays under WiGLE's 180 MiB upload limit. Kismet can only rotate pcapng
# logs by size (pcapng_log_max_mb); kismetdb logs have no such option, but a
# restart makes Kismet open a new log. Run as root by kismet-size-watch.timer.
set -euo pipefail

UNIT="${KISMET_UNIT:-kismet.service}"
MAX_MB="${KISMET_MAX_MB:-150}"

systemctl is-active --quiet "$UNIT" || exit 0

pid=$(systemctl show --property MainPID --value "$UNIT")
[[ "$pid" -gt 0 ]] || exit 0

# Ask Kismet which log it has open instead of picking the newest file: the Pi has
# no RTC, so mtimes from an earlier session can look newer than the live one.
log=""
for fd in /proc/"$pid"/fd/*; do
    target=$(readlink "$fd" 2>/dev/null) || continue
    if [[ "$target" == *.kismet ]]; then
        log="$target"
        break
    fi
done
[[ -n "$log" ]] || exit 0

size=$(stat --format %s "$log")
if (( size > MAX_MB * 1024 * 1024 )); then
    echo "$log is $size bytes, over $MAX_MB MiB; restarting $UNIT"
    systemctl restart "$UNIT"
fi
