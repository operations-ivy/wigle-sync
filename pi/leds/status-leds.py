#!/usr/bin/env python3
"""LED2 and LED3, from what Kismet hears (LED1 is the kernel's: setup-leds.sh).

- LED2 alternates red and white while Kismet heard a Flock ALPR camera or an
  Axon device (body camera?) in the last WINDOW seconds, and breathes green
  otherwise.
- LED3 breathes purple, and blinks once for each device Kismet hears for the
  first time: red for a Wi-Fi access point, blue for a Bluetooth device. A
  busy street gives more than it can show, so each poll's blinks are capped
  and spread out.

Every few seconds this asks Kismet's REST API on localhost for the devices it
heard in the last WINDOW seconds. Flock and Axon come from detect.py's rules
(the same ones the sync counts drives with, so the two can't drift apart).
When Kismet isn't running (at home it stops after 15 minutes) nothing is near
and nothing is new: LED2 breathes green, LED3 purple, and this keeps asking.

The colours that fade (LED2's green, LED3's red and blue) are on the kernel's
software PWM, one pwm-gpio overlay per pin (none of them has hardware PWM);
LED2's red and blue are plain on and off, and LED3's green is unused.

Kismet's login comes from systemd (LoadCredential=kismet-httpd, Kismet's own
kismet_httpd.conf). Nothing is logged about what was heard beyond the kind."""

from __future__ import annotations

import base64
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
FLASH = 1.0  # seconds per colour in LED2's alert: red, white, red, ...
BREATH = 4.0  # seconds per green fade in and out
STEP = 0.03  # seconds per tick
BLINK = 0.12  # seconds LED3 is lit per new device
MAX_BLINKS = 8  # per poll; the rest of a burst isn't shown
ALERT_KINDS = {"flock", "axon"}
CHIP = "/dev/gpiochip0"
LED2_RED, LED2_GREEN, LED2_BLUE = 5, 6, 13
LED3_RED, LED3_GREEN, LED3_BLUE = 17, 27, 22
PWM = Path("/sys/class/pwm")
PERIOD = 10_000_000  # ns: 100 Hz
FIELDS = [
    "kismet.device.base.key",
    "kismet.device.base.macaddr",
    "kismet.device.base.commonname",
    "kismet.device.base.phyname",
    "kismet.device.base.type",
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


def new_devices(devices: list[dict], seen: set[str]) -> tuple[int, int]:
    """Access points and Bluetooth devices not in seen (which this updates)."""
    aps = bts = 0
    for d in devices:
        key = d.get("kismet.device.base.key")
        if not key or key in seen:
            continue
        seen.add(key)
        if d.get("kismet.device.base.phyname") == "IEEE802.11":
            aps += d.get("kismet.device.base.type") == "Wi-Fi AP"
        else:
            bts += 1
    return aps, bts


def blink_plan(aps: int, bts: int, start: float) -> list[tuple[float, str]]:
    """When LED3 lights, and in which colour, for one poll's new devices: at
    most MAX_BLINKS, alternating kinds while both last, spread over the poll."""
    colours = []
    while (aps or bts) and len(colours) < MAX_BLINKS:
        if aps:
            colours.append("red")
            aps -= 1
        if bts and len(colours) < MAX_BLINKS:
            colours.append("blue")
            bts -= 1
    gap = POLL / max(len(colours), 1)
    return [(start + i * gap, c) for i, c in enumerate(colours)]


def breath(t: float) -> float:
    """Green's brightness, 0 to 1, t seconds into the breathing: a smooth
    rise and fall, squared because eyes see brightness roughly that way."""
    phase = (t % BREATH) / BREATH
    level = 1 - abs(2 * phase - 1)
    return level * level


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
    pins = (LED2_RED, LED2_BLUE, LED3_GREEN)
    lines = gpiod.request_lines(
        CHIP,
        consumer="status-leds",
        config={pins: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=off)},
    )
    green2, red3, blue3 = (Fader(pwm_chip(pin)) for pin in (LED2_GREEN, LED3_RED, LED3_BLUE))
    user, password = credentials(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "kismet-httpd")
    near, kismet_up = set(), None
    seen: set[str] | None = None  # None until Kismet first answers; that answer only fills it
    blinks: list[tuple[float, str]] = []
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
            if up and seen is None:
                seen = set()
                new_devices(devices, seen)
            elif up:
                blinks = blink_plan(*new_devices(devices, seen), now)

        if near:
            white = int(now / FLASH) % 2 == 1
            lines.set_values({LED2_RED: on, LED2_BLUE: on if white else off, LED3_GREEN: off})
            green2.set(1.0 if white else 0.0)
        else:
            lines.set_values({LED2_RED: off, LED2_BLUE: off, LED3_GREEN: off})
            green2.set(breath(now))
        lit = next((c for t, c in blinks if t <= now < t + BLINK), None)
        if lit:
            red3.set(1.0 if lit == "red" else 0.0)
            blue3.set(1.0 if lit == "blue" else 0.0)
        else:
            red3.set(breath(now))
            blue3.set(breath(now))
        time.sleep(STEP)


if __name__ == "__main__":
    main()
