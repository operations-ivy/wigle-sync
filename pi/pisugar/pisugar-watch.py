#!/usr/bin/env python3
"""The PiSugar 2 battery under a Pi Zero 2 W wardriver: its level as metrics,
a clean shutdown before it runs flat, and its RTC as the clock's fallback.

It asks pisugar-server (PiSugar's own daemon, which owns the board's I2C
chips) over its Unix socket, one command per line, answered as
"<name>: <value>". setup-pisugar.sh installs the server listening on that
socket only, never on the network.

- Every `poll` seconds: the level, voltage and whether it's charging go to
  node-exporter's textfile collector (pisugar.prom) and to a small JSON
  state file the e-ink display reads.
- Shutdown: once `readings` polls in a row find the level at or under
  `shutdown_percent` (or the voltage at or under `shutdown_volts`) while
  nothing is charging it, it writes why to the state directory (the
  display's last screen shows it) and powers the Pi off. systemd stops
  Kismet first, which closes its logs. pisugar-server's own safe shutdown
  is set lower still, as a backstop for this service failing.
- `--rtc-load` (pisugar-rtc-load.service, once at boot, before chrony and
  Kismet): with no network or GPS yet, sets the clock from the RTC when the
  RTC is ahead of it (fake-hwclock's saved time is the clock's floor).
  While running, once chrony has the real time, the RTC is set from it
  hourly so it never drifts far.

Config: /etc/wigle-pi/handheld.yaml, "battery" (pi/handheld.example.yaml).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import UTC
from pathlib import Path

from handheld_common import chrony_synced
from handheld_common import init_logging
from handheld_common import stop_on_sigterm
from handheld_common import write_atomic

SOCKET = os.environ.get("PISUGAR_SOCKET", "/tmp/pisugar-server.sock")
CONFIG = Path(os.environ.get("HANDHELD_CONFIG", "/etc/wigle-pi/handheld.yaml"))
TEXTFILE = Path(os.environ.get("PISUGAR_METRICS_FILE", "/run/node-exporter-textfile/pisugar.prom"))
STATE_DIR = Path(os.environ.get("WIGLE_PI_STATE", "/run/wigle-pi"))
FAKE_HWCLOCK = Path("/etc/fake-hwclock.data")
DEFAULTS = {
    "poll": 30,
    "shutdown_percent": 10.0,
    "shutdown_volts": 3.50,
    "readings": 4,
    "backstop_percent": 5,
    "rtc_write_every": 3600,
}
RTC_TOLERANCE = 2.0  # seconds: an RTC this close to the clock is left alone


@dataclass(frozen=True)
class Reading:
    """One poll of the battery. None where pisugar-server gave no usable answer."""

    percent: float | None
    volts: float | None
    charging: bool | None

    @property
    def ok(self) -> bool:
        return self.percent is not None


def load_config(path: Path = CONFIG) -> dict:
    """The "battery" section of the handheld config, over DEFAULTS."""
    try:
        import yaml

        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raw = {}
    section = raw.get("battery") or {}
    return {**DEFAULTS, **{k: v for k, v in section.items() if k in DEFAULTS}}


def parse_reply(line: str) -> tuple[str, str] | None:
    """("battery", "85.3") from "battery: 85.3"; None for anything else."""
    name, sep, value = line.strip().partition(":")
    if not sep or not name or " " in name:
        return None
    return name, value.strip()


def parse_float(value: str | None, low: float, high: float) -> float | None:
    """A number within [low, high], else None. An I2C read that failed comes
    back as an error string, or as a value no battery can have."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def parse_bool(value: str | None) -> bool | None:
    return {"true": True, "false": False}.get((value or "").strip().lower())


def reading_from(replies: dict[str, str]) -> Reading:
    """A Reading from pisugar-server's answers by name. The PiSugar 2 reports
    charging as battery_charging; newer models as battery_power_plugged.
    Either one true means it's on external power."""
    flags = [parse_bool(replies.get(k)) for k in ("battery_charging", "battery_power_plugged")]
    if True in flags:
        charging = True
    elif False in flags:
        charging = False
    else:
        charging = None
    return Reading(
        percent=parse_float(replies.get("battery"), 0.0, 100.0),
        volts=parse_float(replies.get("battery_v"), 2.5, 4.5),
        charging=charging,
    )


class ShutdownGuard:
    """Decides when the battery is low enough to power off. It takes `readings`
    low polls in a row (one bad read, or the voltage sagging under a burst of
    load, doesn't count), and any poll that's charging or above both limits
    starts the count again. A poll with no answer neither counts nor resets."""

    def __init__(self, percent: float, volts: float, readings: int):
        self.percent, self.volts, self.readings = percent, volts, readings
        self.low = 0

    def is_low(self, r: Reading) -> bool:
        return (r.percent is not None and r.percent <= self.percent) or (r.volts is not None and r.volts <= self.volts)

    def update(self, r: Reading) -> bool:
        """True when it's time to shut down."""
        if not r.ok and r.volts is None:
            return False
        if r.charging or not self.is_low(r):
            self.low = 0
            return False
        self.low += 1
        return self.low >= self.readings


def render_metrics(r: Reading, shutdown_percent: float) -> str:
    lines = []

    def gauge(name: str, doc: str, value) -> None:
        if value is None:
            return
        lines.extend([f"# HELP {name} {doc}", f"# TYPE {name} gauge", f"{name} {value}"])

    gauge(
        "pi_pisugar_up",
        "1 while the PiSugar gives battery readings (pisugar-server answers and reaches the board).",
        int(r.ok),
    )
    gauge("pi_battery_percent", "The PiSugar battery's level (pisugar-server's estimate from its voltage).", r.percent)
    gauge("pi_battery_volts", "The PiSugar battery's voltage.", r.volts)
    gauge(
        "pi_battery_charging",
        "1 while the PiSugar is on external power.",
        None if r.charging is None else int(r.charging),
    )
    gauge("pi_battery_shutdown_percent", "The level at which the Pi shuts itself down.", shutdown_percent)
    return "\n".join(lines) + "\n"


def rtc_action(
    clock: datetime, rtc: datetime | None, floor: datetime | None, synced: bool, since_write: float | None, every: float
) -> str | None:
    """What to do with the RTC: "rtc2pi" (set the clock from it), "pi2rtc" (set
    it from the clock), or None.

    The clock is trusted once chrony has synced it (internet or GPS); then the
    RTC is written when it's off by more than RTC_TOLERANCE or hasn't been
    written for `every` seconds. Before that, the RTC is trusted only ahead of
    both the clock and fake-hwclock's saved time: an RTC that lost power reads
    some date in 2000, and must never pull the clock back."""
    if synced:
        off = rtc is None or abs((rtc - clock).total_seconds()) > RTC_TOLERANCE
        if off or since_write is None or since_write >= every:
            return "pi2rtc"
        return None
    if rtc is None:
        return None
    if floor is not None and rtc < floor:
        return None
    if (rtc - clock).total_seconds() > RTC_TOLERANCE:
        return "rtc2pi"
    return None


def parse_rtc(value: str | None) -> datetime | None:
    """pisugar-server's RFC 3339 rtc_time, as an aware datetime."""
    try:
        t = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return t if t.tzinfo else None


def fake_hwclock_floor(path: Path = FAKE_HWCLOCK) -> datetime | None:
    """fake-hwclock's saved time ("2026-11-16 21:04:05", UTC)."""
    try:
        return datetime.strptime(path.read_text().strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except (OSError, ValueError):
        return None


class PiSugar:
    """pisugar-server's Unix socket: one command, one answer line."""

    def __init__(self, path: str = SOCKET, timeout: float = 3.0):
        self.path, self.timeout = path, timeout

    def ask(self, command: str) -> str | None:
        """The answer's value, or None if the server didn't answer."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.path)
                s.sendall(command.encode() + b"\n")
                with s.makefile("rb") as f:
                    line = f.readline()
        except OSError:  # no server, or it didn't answer within the timeout
            return None
        parsed = parse_reply(line.decode(errors="replace"))
        return parsed[1] if parsed else None

    def reading(self) -> tuple[Reading, bool]:
        """A Reading, and whether the server answered at all."""
        replies = {}
        for name in ("battery", "battery_v", "battery_charging", "battery_power_plugged"):
            value = self.ask(f"get {name}")
            if value is not None:
                replies[name] = value
        return reading_from(replies), bool(replies)


def rtc_load(pisugar: PiSugar, log) -> None:
    """Once at boot: the clock from the RTC if the RTC is ahead (rtc_action)."""
    rtc = None
    for _ in range(10):  # pisugar-server may still be starting
        rtc = parse_rtc(pisugar.ask("get rtc_time"))
        if rtc is not None:
            break
        time.sleep(1)
    clock = datetime.now(UTC)
    action = rtc_action(clock, rtc, fake_hwclock_floor(), chrony_synced(), None, DEFAULTS["rtc_write_every"])
    if action == "rtc2pi":
        pisugar.ask("rtc_rtc2pi")
        log.info("clock set from the RTC", rtc=rtc.isoformat(), was=clock.isoformat())
    else:
        log.info("clock left alone", rtc=rtc.isoformat() if rtc else None, clock=clock.isoformat())


def publish(reading: Reading, shutdown_percent: float) -> None:
    """The reading as metrics for node-exporter and as state for the display."""
    write_atomic(TEXTFILE, render_metrics(reading, shutdown_percent))
    state = {"percent": reading.percent, "volts": reading.volts, "charging": reading.charging, "at": time.time()}
    write_atomic(STATE_DIR / "battery.json", json.dumps(state))


def sync_rtc(pisugar: PiSugar, last_write: float | None, every: float) -> float | None:
    """Write chrony's time to the RTC when it's due (rtc_action); returns when
    it was last written (time.monotonic())."""
    since = None if last_write is None else time.monotonic() - last_write
    rtc = parse_rtc(pisugar.ask("get rtc_time"))
    if rtc_action(datetime.now(UTC), rtc, None, True, since, every) == "pi2rtc":
        pisugar.ask("rtc_pi2rtc")
        return time.monotonic()
    return last_write


def power_off(reading: Reading, log) -> None:
    """Say why (the display's last screen shows it), then power off."""
    log.warning("battery low: shutting down", percent=reading.percent, volts=reading.volts)
    reason = f"battery {reading.percent:.0f}%" if reading.ok else "battery low"
    write_atomic(STATE_DIR / "shutdown-reason", reason + "\n")
    try:
        result = subprocess.run(["systemctl", "poweroff"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("poweroff failed", error=str(e))
        return
    if result.returncode != 0:
        log.warning("poweroff failed", returncode=result.returncode, stderr=result.stderr.strip())


def board_state(reading: Reading, answered: bool) -> str:
    """What to log when it changes. pisugar-server can answer while the board
    doesn't (its I2C pins not touching: every reply "I2C not connected")."""
    if reading.ok:
        return "battery readings"
    if answered:
        return "pisugar-server answering, but no battery readings (the board isn't on I2C)"
    return "pisugar-server not answering"


def watch(pisugar: PiSugar, config: dict, log, stop: threading.Event) -> None:
    guard = ShutdownGuard(config["shutdown_percent"], config["shutdown_volts"], config["readings"])
    # pisugar-server's own shutdown, under ours, in case this service is down.
    pisugar.ask(f"set_safe_shutdown_level {config['backstop_percent']}")
    pisugar.ask("set_safe_shutdown_delay 60")
    last_write: float | None = None
    was_state, was_charging = None, None
    while not stop.is_set():
        reading, answered = pisugar.reading()
        state = board_state(reading, answered)
        if state != was_state:
            log.info(state)
            was_state = state
        if reading.charging is not None and reading.charging != was_charging:
            log.info("power", charging=reading.charging, percent=reading.percent, volts=reading.volts)
            was_charging = reading.charging
        publish(reading, config["shutdown_percent"])
        if guard.update(reading):
            power_off(reading, log)
            return
        if reading.ok and chrony_synced():
            last_write = sync_rtc(pisugar, last_write, config["rtc_write_every"])
        stop.wait(config["poll"])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rtc-load", action="store_true", help="set the clock from the RTC if it's ahead, and exit")
    args = parser.parse_args(argv)
    log = init_logging("pisugar-watch")
    pisugar = PiSugar()
    if args.rtc_load:
        rtc_load(pisugar, log)
    else:
        watch(pisugar, load_config(), log, stop_on_sigterm())


if __name__ == "__main__":
    sys.exit(main())
