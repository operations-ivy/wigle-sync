#!/usr/bin/env python3
"""LED2 from what Kismet hears, LED3 from the SoC's temperature (LED1 is the
kernel's: setup-leds.sh).

- LED2 alternates red and white while Kismet heard a Flock ALPR camera or an
  Axon device (body camera?) in the last WINDOW seconds, and is off otherwise.
- LED3 shows how hot the Pi is: green when cool (as after a boot), yellow
  when warm, orange when it has run hot for a while (the average, so a brief
  dip doesn't turn it yellow), and flashing red close to the firmware's hard
  throttle at 80 °C. A Pi 3B+ slows its CPU from 60 °C (its soft limit).
  Each level needs HYSTERESIS degrees under its threshold to clear, so a
  reading on the line doesn't flicker between two colours.

Every few seconds this asks Kismet's REST API on localhost for the devices it
heard in the last WINDOW seconds, and reads the temperature. Flock and Axon
come from detect.py's rules (the same ones the sync counts drives with, so the
two can't drift apart). When Kismet isn't running (at home it stops after 15
minutes) nothing is near, LED2 is off, and this keeps asking.

LED3's red and green are on the kernel's software PWM, one pwm-gpio overlay
per pin (none of them has hardware PWM), so they mix into yellow and orange;
LED2 is plain on and off, and LED3's blue is unused.

Kismet's login comes from systemd (LoadCredential=kismet-httpd, Kismet's own
kismet_httpd.conf). Nothing is logged about what was heard beyond the kind."""

from __future__ import annotations

import base64
import collections
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from detect import kind_of

KISMET = "http://127.0.0.1:2501"
WINDOW = 60  # seconds a device counts as near after Kismet last heard it
POLL = 3  # seconds between asks
FLASH = 1.0  # seconds per colour in LED2's alert (red, white, ...) and LED3's red
STEP = 0.03  # seconds per tick
THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")  # millidegrees C
AVERAGE = 300  # seconds of readings in the average
WARM = 55.0  # °C, average: yellow
HOT = 65.0  # °C, average: orange
CRITICAL = 75.0  # °C, now: red (the firmware throttles hard at 80)
HYSTERESIS = 2.0  # °C under a threshold before its level clears
LEVELS = ("green", "yellow", "orange", "red")
# LED3's red and green duty per level; the modules' green is the brighter.
COLOURS = {"green": (0.0, 1.0), "yellow": (1.0, 0.5), "orange": (1.0, 0.15), "red": (1.0, 0.0)}
ALERT_KINDS = {"flock", "axon"}
CHIP = "/dev/gpiochip0"
LED2_RED, LED2_GREEN, LED2_BLUE = 5, 6, 13
LED3_RED, LED3_GREEN, LED3_BLUE = 17, 27, 22
PWM = Path("/sys/class/pwm")
PERIOD = 10_000_000  # ns: 100 Hz
FIELDS = [
    "kismet.device.base.macaddr",
    "kismet.device.base.commonname",
    "kismet.device.base.phyname",
]


def credentials(path: Path) -> tuple[str, str]:
    """httpd_username and httpd_password from a kismet_httpd.conf."""
    conf = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            conf[key.strip()] = value.strip()
    return conf["httpd_username"], conf["httpd_password"]


def kinds_near(devices: list[dict]) -> set[str]:
    """The alerting kinds among Kismet's device records."""
    found = set()
    for d in devices:
        phy = "WIFI" if d.get("kismet.device.base.phyname") == "IEEE802.11" else "BT"
        kind = kind_of(d.get("kismet.device.base.macaddr", ""), d.get("kismet.device.base.commonname", ""), phy)
        if kind in ALERT_KINDS:
            found.add(kind)
    return found


def heat_level(now: float, average: float, previous: str | None = None) -> str:
    """LED3's level for the temperature now and its recent average (°C). A
    level reached stays until its reading is HYSTERESIS under the threshold."""

    def over(level: str, value: float, threshold: float) -> bool:
        held = previous is not None and LEVELS.index(previous) >= LEVELS.index(level)
        return value >= threshold - (HYSTERESIS if held else 0)

    if over("red", now, CRITICAL):
        return "red"
    if over("orange", average, HOT):
        return "orange"
    if over("yellow", average, WARM):
        return "yellow"
    return "green"


def read_temperature(path: Path = THERMAL) -> float | None:
    """The SoC's temperature in °C, or None if it can't be read."""
    try:
        return int(path.read_text()) / 1000
    except (OSError, ValueError):
        return None


def pwm_chip(pin: int, root: Path = PWM) -> Path:
    """The pwmchip a pwm-gpio overlay made for a pin (its device is named
    pwm_gpio@<pin in hex>; the chips are numbered in load order)."""
    for chip in sorted(root.glob("pwmchip*")):
        if (chip / "device").resolve().name == f"pwm_gpio@{pin:x}":
            return chip
    raise FileNotFoundError(f"no pwm-gpio chip for GPIO{pin}")


class Fader:
    """One colour on a pwm-gpio chip's one channel, 0 (off) to 1 (full)."""

    def __init__(self, chip: Path):
        self.pwm = chip / "pwm0"
        if not self.pwm.exists():
            (chip / "export").write_text("0")
        (self.pwm / "duty_cycle").write_text("0")
        (self.pwm / "period").write_text(str(PERIOD))
        (self.pwm / "enable").write_text("1")
        self.duty = 0

    def set(self, level: float) -> None:
        duty = round(PERIOD * min(1.0, max(0.0, level)))
        if duty != self.duty:
            (self.pwm / "duty_cycle").write_text(str(duty))
            self.duty = duty


def recent_devices(user: str, password: str) -> list[dict]:
    body = urllib.parse.urlencode({"json": json.dumps({"fields": FIELDS})}).encode()
    req = urllib.request.Request(f"{KISMET}/devices/last-time/-{WINDOW}/devices.json", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    with urllib.request.urlopen(req, timeout=POLL) as resp:
        return json.load(resp)


def main() -> None:
    import gpiod
    from gpiod.line import Direction, Value

    on, off = Value.ACTIVE, Value.INACTIVE
    pins = (LED2_RED, LED2_GREEN, LED2_BLUE, LED3_BLUE)
    lines = gpiod.request_lines(
        CHIP,
        consumer="status-leds",
        config={pins: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=off)},
    )
    red3, green3 = (Fader(pwm_chip(pin)) for pin in (LED3_RED, LED3_GREEN))
    user, password = credentials(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "kismet-httpd")
    near, kismet_up = set(), None
    readings: collections.deque[float] = collections.deque(maxlen=AVERAGE // POLL)
    level: str | None = None
    next_poll = 0.0
    while True:
        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + POLL
            try:
                devices = recent_devices(user, password)
                up = True
            except OSError:  # Kismet stopped (home) or not up yet
                devices, up = [], False
            if up != kismet_up:
                print("kismet answering" if up else "kismet not answering", flush=True)
                kismet_up = up
            found = kinds_near(devices)
            if found != near:
                print(f"near: {', '.join(sorted(found)) or 'nothing'}", flush=True)
                near = found
            temperature = read_temperature()
            if temperature is not None:
                readings.append(temperature)
                average = sum(readings) / len(readings)
                new = heat_level(temperature, average, level)
                if new != level:
                    print(f"heat: {new} ({temperature:.1f} °C, average {average:.1f} °C)", flush=True)
                    level = new

        flash = int(now / FLASH) % 2 == 1
        if near:
            white = on if flash else off
            lines.set_values({LED2_RED: on, LED2_GREEN: white, LED2_BLUE: white, LED3_BLUE: off})
        else:
            lines.set_values({LED2_RED: off, LED2_GREEN: off, LED2_BLUE: off, LED3_BLUE: off})
        red, green = COLOURS[level] if level else (0.0, 0.0)
        if level == "red" and flash:
            red = 0.0
        red3.set(red)
        green3.set(green)
        time.sleep(STEP)


if __name__ == "__main__":
    main()
