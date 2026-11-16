#!/usr/bin/env python3
"""The wardriver's status on its e-ink display (a Waveshare 2.13", as on a
pwnagotchi): what matters on the road, readable at a glance.

    AWAY            GPS 3D 9 sat
    Wi-Fi ok   BT ok
    Heard 245 Wi-Fi  30 BT
    Battery 78%
    14:32

- Home or away: on the home Wi-Fi or not (on-home-wifi, as pi-metrics uses).
- GPS: gpsd's fix (none, 2D, 3D) and the satellites in it.
- Kismet's capture sources: ok, ERR (in error), off; or "Kismet stopped"
  (at home it stops 15 minutes after joining the Wi-Fi).
- Devices Kismet heard in the last minute, by Wi-Fi and Bluetooth, rounded
  so the screen doesn't change on every count.
- The PiSugar's battery from pisugar-watch's state file ("+" while charging).
- The time, with "?" until chrony has the real time.

E-ink wears with refreshes and ghosts with partial ones, so the screen is
redrawn only when its text changes, by partial refresh at most every
`min_interval` seconds, and by full refresh after `full_every` partial ones
or `full_interval` seconds, whichever comes first (RefreshPolicy). The
panel stays initialised between draws, as pwnagotchi runs it. On stop
(shutdown, or the service stopping) a last full refresh says why, and the
panel is put to deep sleep: that screen stays while the Pi is off.

Waveshare's own Python driver for the panel (waveshare_epd, fetched pinned by
setup-eink.sh) does the SPI and GPIO. The model is config: /etc/wigle-pi/
handheld.yaml, "display" (pi/handheld.example.yaml).
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path

import structlog

CONFIG = Path(os.environ.get("HANDHELD_CONFIG", "/etc/wigle-pi/handheld.yaml"))
STATE_DIR = Path(os.environ.get("WIGLE_PI_STATE", "/run/wigle-pi"))
KISMET = "http://127.0.0.1:2501"
RECENT = 60  # seconds: "heard in the last minute", as pi-metrics counts it
BATTERY_STALE = 180  # seconds: an older battery reading isn't shown
MODELS = ("epd2in13_V2", "epd2in13_V3", "epd2in13_V4")
DEFAULTS = {
    "model": "epd2in13_V4",
    "rotate": 0,
    "poll": 10,
    "min_interval": 30,
    "full_every": 30,
    "full_interval": 1800,
}
WIDTH, HEIGHT = 250, 122  # the 2.13" panels, landscape
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@dataclass
class Status:
    """Everything the screen shows. None where it isn't known."""

    home: bool | None = None
    gps_mode: int = 0  # gpsd: 0 unknown, 1 no fix, 2 2D, 3 3D
    satellites: int = 0
    kismet_up: bool = False
    sources: dict[str, str] = field(default_factory=dict)  # name: ok, ERR, off
    recent: dict[str, int] | None = None  # wifi, bluetooth
    battery_percent: float | None = None
    charging: bool | None = None
    clock_synced: bool = False
    now: datetime = field(default_factory=datetime.now)


def load_config(path: Path = CONFIG) -> dict:
    """The "display" section of the handheld config, over DEFAULTS."""
    try:
        import yaml

        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raw = {}
    section = raw.get("display") or {}
    config = {**DEFAULTS, **{k: v for k, v in section.items() if k in DEFAULTS}}
    if config["model"] not in MODELS:
        raise ValueError(f"display model {config['model']!r} isn't one of {', '.join(MODELS)}")
    if config["rotate"] not in (0, 180):
        raise ValueError("display rotate is 0 or 180")
    return config


def rounded(n: int) -> int:
    """Counts as shown: exact under 20, to 5 under 100, then to 10, so a
    busy street doesn't redraw the screen on every poll."""
    if n < 20:
        return n
    step = 5 if n < 100 else 10
    return int(round(n / step) * step)


def source_state(src: dict) -> str:
    """One Kismet datasource record as ok, ERR or off."""
    if src.get("kismet.datasource.error"):
        return "ERR"
    return "ok" if src.get("kismet.datasource.running") else "off"


def source_label(name: str) -> str:
    """Kismet's source names (kismet_site.conf: wlan_mon0, usb_bt) as shown."""
    return "BT" if "bt" in name.lower() or name.startswith("hci") else "Wi-Fi"


def screen_lines(s: Status) -> list[str]:
    """The screen's text, top to bottom: the first line is drawn bold, the
    last small. Pure, so the layout is tested without a panel."""
    where = {True: "HOME", False: "AWAY", None: "?"}[s.home]
    fix = {2: "2D", 3: "3D"}.get(s.gps_mode)
    gps = f"GPS {fix} {s.satellites} sat" if fix else "GPS no fix" if s.gps_mode == 1 else "GPS ?"
    if not s.kismet_up:
        sources = "Kismet stopped"
    elif not s.sources:
        sources = "Kismet: no sources"
    else:
        sources = "   ".join(f"{source_label(n)} {state}" for n, state in sorted(s.sources.items()))
    if s.recent is None:
        heard = "Heard -"
    else:
        heard = f"Heard {rounded(s.recent.get('wifi', 0))} Wi-Fi  {rounded(s.recent.get('bluetooth', 0))} BT"
    if s.battery_percent is None:
        battery = "Battery ?"
    else:
        battery = f"Battery {s.battery_percent:.0f}%" + (" +" if s.charging else "")
    clock = s.now.strftime("%H:%M") + ("" if s.clock_synced else " ?")
    return [f"{where}  {gps}", sources, heard, battery, clock]


def off_lines(reason: str | None, shutting_down: bool, now: datetime, clock_synced: bool) -> list[str]:
    """The last screen, left on the panel while the Pi is off. `reason` is
    pisugar-watch's (a low battery); otherwise the Pi is shutting down for
    some other reason, or only the display service stopped."""
    at = now.strftime("%Y-%m-%d %H:%M") + ("" if clock_synced else " ?")
    if reason:
        return ["OFF", reason, at, "", "wigle-sync"]
    if shutting_down:
        return ["OFF", "shut down", at, "", "wigle-sync"]
    return ["DISPLAY STOPPED", "the Pi may still be on", at, "", "wigle-sync"]


class RefreshPolicy:
    """When to redraw: None (leave it), "partial" or "full"."""

    def __init__(self, min_interval: float, full_every: int, full_interval: float):
        self.min_interval, self.full_every, self.full_interval = min_interval, full_every, full_interval
        self.shown: list[str] | None = None
        self.last = self.last_full = float("-inf")
        self.partials = 0

    def decide(self, lines: list[str], now: float) -> str | None:
        if self.shown is None:
            return "full"
        if lines == self.shown or now - self.last < self.min_interval:
            return None
        if self.partials >= self.full_every or now - self.last_full >= self.full_interval:
            return "full"
        return "partial"

    def done(self, lines: list[str], kind: str, now: float) -> None:
        self.shown, self.last = list(lines), now
        if kind == "full":
            self.last_full, self.partials = now, 0
        else:
            self.partials += 1


def gps(timeout: float = 3.0) -> tuple[int, int]:
    """gpsd's fix mode and satellites used (as pi-metrics reads them)."""
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


def credentials(path: Path) -> tuple[str, str] | None:
    """httpd_username and httpd_password from a kismet_httpd.conf."""
    conf = {}
    try:
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                conf[key.strip()] = value.strip()
    except OSError:
        return None
    if "httpd_username" not in conf or "httpd_password" not in conf:
        return None
    return conf["httpd_username"], conf["httpd_password"]


def kismet_get(path: str, login: tuple[str, str], fields: list[str] | None = None):
    body = urllib.parse.urlencode({"json": json.dumps({"fields": fields})}).encode() if fields else None
    req = urllib.request.Request(f"{KISMET}{path}", data=body)
    req.add_header("Authorization", "Basic " + base64.b64encode(":".join(login).encode()).decode())
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def count_recent(devices: list[dict]) -> dict[str, int]:
    counts = {"wifi": 0, "bluetooth": 0}
    for d in devices:
        phy = d.get("kismet.device.base.phyname", "")
        if phy == "IEEE802.11":
            counts["wifi"] += 1
        elif phy in ("Bluetooth", "BTLE"):
            counts["bluetooth"] += 1
    return counts


def battery(path: Path, now: float) -> tuple[float | None, bool | None]:
    """pisugar-watch's last reading, if recent."""
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None
    if now - float(state.get("at", 0)) > BATTERY_STALE:
        return None, None
    return state.get("percent"), state.get("charging")


def run_ok(*cmd: str) -> bool | None:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def clock_synced() -> bool:
    try:
        r = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    fields = r.stdout.strip().split(",")
    return (
        r.returncode == 0
        and len(fields) > 1
        and fields[0] not in ("", "00000000", "7F7F0101")
        and (fields[-1] != "Not synchronised")
    )


def collect(login: tuple[str, str] | None) -> Status:
    mode, sats = gps()
    status = Status(home=run_ok("/usr/local/sbin/on-home-wifi"), gps_mode=mode, satellites=sats)
    if login:
        try:
            status.sources = {
                src["kismet.datasource.name"]: source_state(src)
                for src in kismet_get("/datasource/all_sources.json", login)
                if src.get("kismet.datasource.name")
            }
            status.recent = count_recent(
                kismet_get(f"/devices/last-time/-{RECENT}/devices.json", login, ["kismet.device.base.phyname"])
            )
            status.kismet_up = True
        except (OSError, ValueError):
            pass
    status.battery_percent, status.charging = battery(STATE_DIR / "battery.json", time.time())
    status.clock_synced = clock_synced()
    status.now = datetime.now()
    return status


def draw(lines: list[str], rotate: int = 0):
    """The lines as a 1-bit landscape image for the panel."""
    from PIL import Image
    from PIL import ImageDraw
    from PIL import ImageFont

    image = Image.new("1", (WIDTH, HEIGHT), 255)
    pen = ImageDraw.Draw(image)
    bold, body, small = ImageFont.truetype(FONT_BOLD, 18), ImageFont.truetype(FONT, 16), ImageFont.truetype(FONT, 13)
    y = 2
    for i, line in enumerate(lines):
        font = bold if i == 0 else small if i == len(lines) - 1 else body
        pen.text((4, y), line, font=font, fill=0)
        y += 24 if i == 0 else 21
    if lines and lines[0] not in ("OFF", "DISPLAY STOPPED"):
        pen.line((0, 24, WIDTH, 24), fill=0)
    return image.rotate(180) if rotate == 180 else image


class Panel:
    """One of Waveshare's 2.13" panels. The V2 driver takes the refresh mode
    at init; V3 and V4 reset themselves for each partial refresh. A full
    refresh also stores the image the partial ones are drawn against."""

    def __init__(self, model: str):
        self.module = importlib.import_module(f"waveshare_epd.{model}")
        self.epd = self.module.EPD()
        self.v2 = model == "epd2in13_V2"

    def full(self, image) -> None:
        if self.v2:
            self.epd.init(self.epd.FULL_UPDATE)
        else:
            self.epd.init()
        self.epd.displayPartBaseImage(self.epd.getbuffer(image))
        if self.v2:
            self.epd.init(self.epd.PART_UPDATE)

    def partial(self, image) -> None:
        self.epd.displayPartial(self.epd.getbuffer(image))

    def sleep(self) -> None:
        self.epd.sleep()


def init_logging() -> structlog.typing.FilteringBoundLogger:
    """JSON lines when the unit sets LOG_FORMAT=json, as status-leds does."""
    json_out = os.environ.get("LOG_FORMAT") == "json"
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            lambda _, __, event_dict: {**event_dict, "logger": "eink-status", "service": "eink-status"},
            structlog.processors.JSONRenderer() if json_out else structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return structlog.get_logger()


def main() -> None:
    log = init_logging()
    config = load_config()
    panel = Panel(config["model"])
    policy = RefreshPolicy(config["min_interval"], config["full_every"], config["full_interval"])
    creds = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/nonexistent")) / "kismet-httpd"
    stop = False

    def on_term(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_term)
    log.info("started", model=config["model"], min_interval=config["min_interval"], full_every=config["full_every"])
    try:
        while not stop:
            status = collect(credentials(creds))
            lines = screen_lines(status)
            now = time.monotonic()
            kind = policy.decide(lines, now)
            if kind:
                image = draw(lines, config["rotate"])
                if kind == "full":
                    panel.full(image)
                else:
                    panel.partial(image)
                policy.done(lines, kind, now)
            end = time.monotonic() + config["poll"]
            while not stop and time.monotonic() < end:
                time.sleep(0.5)
    finally:
        try:
            reason = (STATE_DIR / "shutdown-reason").read_text().strip()
        except OSError:
            reason = None
        shutting_down = (
            subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True).stdout.strip()
            == "stopping"
        )
        panel.full(draw(off_lines(reason, shutting_down, datetime.now(), clock_synced()), config["rotate"]))
        panel.sleep()
        log.info("stopped", reason=reason)


if __name__ == "__main__":
    sys.exit(main())
