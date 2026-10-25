#!/usr/bin/env python3
"""The wardriving Pi's own readings, for node-exporter's textfile collector:
Kismet's open log size and the logs waiting for wigle-sync, the GPS fix,
whether the USB Bluetooth dongle is Kismet's hci0, how each of Kismet's
capture sources is doing and what it heard in the last minute (#48), the
Pi's power and throttling flags, and whether it's on the home Wi-Fi. Run
every 30 s by pi-metrics.timer (as root, to read Kismet's open files);
writes to tmpfs, so the SD card isn't worn by it.

Away from home nothing can reach Prometheus: vmagent beside it keeps these
on disk and remote-writes them once the Pi is home (README, "Metrics").
"""
from __future__ import annotations

import base64
import glob
import json
import os
import socket
import subprocess
import tempfile
import urllib.parse
import urllib.request

OUT = os.environ.get("PI_METRICS_FILE", "/run/node-exporter-textfile/pi.prom")
LOG_DIR = os.environ.get("KISMET_LOG_DIR", "/var/log/kismet")
KISMET = os.environ.get("KISMET_URL", "http://127.0.0.1:2501")
# Kismet's web login (pi-metrics.service: LoadCredential=kismet-httpd).
KISMET_LOGIN = os.path.join(os.environ.get("CREDENTIALS_DIRECTORY", "/nonexistent"), "kismet-httpd")
RECENT = 60  # seconds: "heard in the last minute", as LED2 counts it


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


def kismet_login(path=None):
    """httpd_username and httpd_password from a kismet_httpd.conf, or None."""
    conf = {}
    try:
        with open(path or KISMET_LOGIN) as f:
            for line in f:
                key, sep, value = line.partition("=")
                if sep:
                    conf[key.strip()] = value.strip()
    except OSError:
        return None
    if "httpd_username" not in conf or "httpd_password" not in conf:
        return None
    return conf["httpd_username"], conf["httpd_password"]


def kismet_get(path, login, fields=None, timeout=5.0):
    """One call to Kismet's REST API; None if Kismet isn't answering."""
    body = urllib.parse.urlencode({"json": json.dumps({"fields": fields})}).encode() if fields else None
    req = urllib.request.Request(f"{KISMET}{path}", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(":".join(login).encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (OSError, ValueError):
        return None


def kismet_sources(login):
    """Each capture source by name (usb_bt, wlan_mon0): whether it's running
    or in error, its retries and its packet count. Empty when Kismet isn't up."""
    out = {}
    for src in kismet_get("/datasource/all_sources.json", login) or []:
        name = src.get("kismet.datasource.name")
        if name:
            out[name] = {
                "running": int(src.get("kismet.datasource.running", 0)),
                "error": int(src.get("kismet.datasource.error", 0)),
                "retries": int(src.get("kismet.datasource.total_retry_attempts", 0)),
                "packets": int(src.get("kismet.datasource.num_packets", 0)),
            }
    return out


def kismet_recent(login):
    """Devices Kismet heard in the last RECENT seconds, by phy (wifi, bluetooth),
    or None when Kismet isn't answering. Counts only."""
    devices = kismet_get(f"/devices/last-time/-{RECENT}/devices.json", login, ["kismet.device.base.phyname"])
    if devices is None:
        return None
    counts = {"wifi": 0, "bluetooth": 0}
    for d in devices:
        phy = d.get("kismet.device.base.phyname", "")
        key = "wifi" if phy == "IEEE802.11" else "bluetooth" if phy in ("Bluetooth", "BTLE") else "other"
        counts[key] = counts.get(key, 0) + 1
    return counts


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

    def gauge(name, doc, value, label=None):
        """value, or {label value: value} for one series per label value."""
        if value is None or value == {}:
            return
        lines.extend([f"# HELP {name} {doc}", f"# TYPE {name} gauge"])
        for key, v in sorted(value.items()) if isinstance(value, dict) else [(None, value)]:
            lines.append(f'{name}{{{label}="{key}"}} {v}' if key is not None else f"{name} {v}")

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
    login = kismet_login() if log_bytes is not None else None
    if login:
        sources = kismet_sources(login)
        for field, doc in (
            ("running", "1 while Kismet's capture source is running."),
            ("error", "1 while Kismet's capture source is in error."),
            ("retries", "Times Kismet has tried to reopen the source this session."),
            ("packets", "Packets the source has captured this session (falls to 0 when Kismet restarts)."),
        ):
            gauge(f"pi_kismet_source_{field}", doc, {n: v[field] for n, v in sources.items()}, "source")
        gauge(
            "pi_kismet_recent_devices",
            f"Devices Kismet heard in the last {RECENT} s, by phy.",
            kismet_recent(login),
            "phy",
        )
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
