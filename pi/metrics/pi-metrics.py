#!/usr/bin/env python3
"""The wardriving Pi's own readings, for node-exporter's textfile collector:
Kismet's open log size and the logs waiting for wigle-sync, the GPS fix,
whether the USB Bluetooth dongle is Kismet's hci0, the Pi's power and
throttling flags, and whether it's on the home Wi-Fi. Run every 30 s by pi-metrics.timer (as root, to read
Kismet's open files); writes to tmpfs, so the SD card isn't worn by it.

Away from home nothing can reach Prometheus: vmagent beside it keeps these
on disk and remote-writes them once the Pi is home (README, "Metrics").
"""
from __future__ import annotations

import glob
import json
import os
import socket
import subprocess
import tempfile

OUT = os.environ.get("PI_METRICS_FILE", "/run/node-exporter-textfile/pi.prom")
LOG_DIR = os.environ.get("KISMET_LOG_DIR", "/var/log/kismet")


def run(*cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def kismet_log_bytes():
    """The size of the .kismet log Kismet has open, or None when it isn't running."""
    r = run("systemctl", "show", "--property", "MainPID", "--value", "kismet.service")
    pid = r.stdout.strip() if r else ""
    if not pid or pid == "0":
        return None
    for fd in glob.glob(f"/proc/{pid}/fd/*"):
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target.endswith(".kismet"):
            try:
                return os.path.getsize(target)
            except OSError:
                return None
    return 0


def pending_logs():
    """Finished .kismet logs waiting in the log directory for wigle-sync."""
    files = [f for f in glob.glob(os.path.join(LOG_DIR, "*.kismet")) if os.path.isfile(f)]
    return len(files), sum(os.path.getsize(f) for f in files)


def gps(timeout=3.0):
    """gpsd's fix mode (0 unknown, 1 none, 2 2D, 3 3D) and satellites used."""
    mode, used = 0, 0
    try:
        with socket.create_connection(("127.0.0.1", 2947), timeout=timeout) as s:
            s.sendall(b'?WATCH={"enable":true,"json":true}\n')
            f = s.makefile("rb")
            seen = set()
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("class") == "TPV":
                    mode = int(msg.get("mode", 0))
                    seen.add("TPV")
                elif msg.get("class") == "SKY":
                    sats = msg.get("satellites") or []
                    used = int(msg.get("uSat", sum(1 for x in sats if x.get("used"))))
                    seen.add("SKY")
                if seen == {"TPV", "SKY"}:
                    break
    except (OSError, ValueError):
        pass
    return mode, used


def bt_dongle(uevent="/sys/class/bluetooth/hci0/device/uevent"):
    """1 when hci0 is a USB adapter (the long-range dongle), 0 when it's
    something else (the onboard chip, if disable-bt didn't take), None when
    there's no hci0 at all."""
    try:
        with open(uevent) as f:
            return int("DRIVER=btusb" in f.read().split())
    except OSError:
        return None


def throttled():
    """vcgencmd's flags: under-voltage and throttling, now and since boot."""
    r = run("vcgencmd", "get_throttled")
    if r is None or "=" not in r.stdout:
        return None
    return int(r.stdout.strip().split("=", 1)[1], 16)


def home():
    r = run("/usr/local/sbin/on-home-wifi")
    return None if r is None else int(r.returncode == 0)


def render():
    lines = []

    def gauge(name, doc, value):
        if value is None:
            return
        lines.extend([f"# HELP {name} {doc}", f"# TYPE {name} gauge", f"{name} {value}"])

    log_bytes = kismet_log_bytes()
    gauge("pi_kismet_running", "1 while Kismet runs (only away from home).", int(log_bytes is not None))
    gauge("pi_kismet_log_bytes", "The size of the log Kismet is writing.", log_bytes)
    count, size = pending_logs()
    gauge("pi_kismet_pending_logs", "Finished Kismet logs waiting for wigle-sync.", count)
    gauge("pi_kismet_pending_bytes", "Their size.", size)
    mode, used = gps()
    gauge("pi_gps_fix_mode", "gpsd's fix: 0 unknown, 1 none, 2 2D, 3 3D.", mode)
    gauge("pi_gps_satellites_used", "Satellites in the fix.", used)
    gauge("pi_bt_dongle", "1 while Kismet's hci0 is the USB Bluetooth dongle, 0 if it's another adapter.", bt_dongle())
    gauge("pi_home_wifi", "1 while the Pi is on the home Wi-Fi.", home())
    flags = throttled()
    if flags is not None:
        for bit, name in ((0, "under_voltage"), (1, "freq_capped"), (2, "throttled"), (3, "soft_temp_limit")):
            gauge(f"pi_{name}", f"1 while the firmware reports {name.replace('_', ' ')}.", (flags >> bit) & 1)
            gauge(
                f"pi_{name}_since_boot",
                f"1 if {name.replace('_', ' ')} happened since boot.",
                (flags >> (bit + 16)) & 1,
            )
    return "\n".join(lines) + "\n"


def main():
    text = render()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), prefix=".pi-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


if __name__ == "__main__":
    main()
