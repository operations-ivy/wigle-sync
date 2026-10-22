#!/usr/bin/env python3
"""LED2: flash red and white while Kismet hears a Flock ALPR camera or an Axon
device (body camera?) on the road; otherwise breathe green.

Every few seconds this asks Kismet's REST API on localhost for the devices it
has heard in the last WINDOW seconds and runs them through detect.py's rules
(the same ones the sync counts drives with, so the two can't drift apart).
Any Flock or Axon among them and LED2 alternates red and white; otherwise it
fades green in and out. When Kismet isn't running (at home it stops after 15
minutes) nothing is near, so it breathes green, and this keeps asking.

Green fades, so it's on the kernel's software PWM (the pwm-gpio overlay on
GPIO6, which has no hardware PWM); red and blue are plain on and off.

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
FLASH = 1.0  # seconds per colour: red, white, red, ...
BREATH = 4.0  # seconds per green fade in and out
STEP = 0.03  # seconds between brightness changes while breathing
ALERT_KINDS = {"flock", "axon"}
CHIP = "/dev/gpiochip0"
RED, BLUE = 5, 13  # LED2, the middle module; its green (GPIO6) is the PWM
PWM = Path("/sys/class/pwm/pwmchip0")
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


def breath(t: float) -> float:
    """Green's brightness, 0 to 1, t seconds into the breathing: a smooth
    rise and fall, squared because eyes see brightness roughly that way."""
    phase = (t % BREATH) / BREATH
    level = 1 - abs(2 * phase - 1)
    return level * level


class Green:
    """LED2's green on pwmchip0's one channel (pwm-gpio overlay, GPIO6)."""

    def __init__(self, chip: Path = PWM):
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
    lines = gpiod.request_lines(
        CHIP,
        consumer="led2-alert",
        config={(RED, BLUE): gpiod.LineSettings(direction=Direction.OUTPUT, output_value=off)},
    )
    green = Green()
    user, password = credentials(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "kismet-httpd")
    near, kismet_up, white = set(), None, False
    next_poll = 0.0
    while True:
        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + POLL
            try:
                found = kinds_near(recent_devices(user, password))
                up = True
            except OSError:  # Kismet stopped (home) or not up yet
                found, up = set(), False
            if up != kismet_up:
                print("kismet answering" if up else "kismet not answering; breathing green", flush=True)
                kismet_up = up
            if found != near:
                print(f"near: {', '.join(sorted(found)) or 'nothing'}", flush=True)
                near = found
        if near:
            white = not white
            lines.set_values({RED: on, BLUE: on if white else off})
            green.set(1.0 if white else 0.0)
            time.sleep(FLASH)
        else:
            lines.set_values({RED: off, BLUE: off})
            green.set(breath(now))
            time.sleep(STEP)


if __name__ == "__main__":
    main()
