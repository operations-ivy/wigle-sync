#!/usr/bin/env python3
"""The wardriver's status on its e-ink display (a Waveshare 2.13", as on a
pwnagotchi): what matters on the road, readable at a glance. Three things
(the user, 2026-11-20): the time, the GPS fix (the one thing to wait for),
and anything wrong with what wardriving needs.

    +-[a chip in each corner]-----------------------+
    | 14:32               |                         |
    | GPS 3D 9 SAT        |         (⌐■_■)          |
    | NO ERRORS           |                         |
    |                     |          cool           |
    +-----------------------------------------------+

- The time, larger, with "?" until chrony has the real time.
- GPS: gpsd's fix (none, 2D, 3D) and the satellites in it.
- Errors, or NO ERRORS: Kismet down away from home (at home it stops 15
  minutes after joining the Wi-Fi), no capture sources, a source in error
  or off (BT, WIFI; two Wi-Fi radios by band, 2G and 5G), and the PiSugar's
  battery at LOW_BATTERY or under while not charging (pisugar-watch's state
  file). On two lines when they don't fit on one.
- The look: DotGothic16, a pixel font drawn on a 16 px grid (the time at
  twice that), so every glyph lands on whole pixels; a chip in each corner
  (eink_art.py).
- The mascot on the right, pwnagotchi's stock text face for each of its 25
  emotions (eink_art.py; the user, 2026-11-20), picked by emotion():
  cool capturing with a fix (excited when it hears a lot), looking left and
  right while it waits for one, sleep at home with Kismet stopped (upload
  while sessions wait to go), broken with a source down, sad with Kismet
  stopped away from home, demotivated on a low battery, awake on the first
  screen. Every screen has one: an error reading Kismet, gpsd or the battery
  shows an ERROR screen with debug, and the last screen, left while the Pi
  is off, sleeps, is demotivated after a low-battery shutdown, or broken if
  the display crashed.

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
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from pathlib import Path
from typing import Literal

from eink_art import BORDER
from eink_art import COLUMN
from eink_art import corner_chips
from eink_art import draw_face
from eink_art import Emotion
from eink_art import label
from handheld_common import chrony_synced
from handheld_common import init_logging
from handheld_common import stop_on_sigterm

Refresh = Literal["full", "partial"]

CONFIG = Path(os.environ.get("HANDHELD_CONFIG", "/etc/wigle-pi/handheld.yaml"))
STATE_DIR = Path(os.environ.get("WIGLE_PI_STATE", "/run/wigle-pi"))
KISMET = "http://127.0.0.1:2501"
WIFI_BANDS = {"wlan_mon0": "5G", "wlan_mon1": "2G"}  # WIFI_SPLIT's radios, by band
# Where Kismet logs and wigle-sync collects (its PI_REMOTE_DIR, PI_ARCHIVE_DIR).
CAPTURE_DIR = Path(os.environ.get("KISMET_LOG_DIR", "/var/log/kismet"))
LOW_BATTERY = 20  # percent: the mascot is demotivated at or under this, not charging
EXCITED = 400  # devices heard in the last minute: cool to excited (brick69: median 251, max 601)
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
# Terminus, the Linux console's bitmap font (fonts-terminus-otb; the user,
# 2026-11-17: retro): crisp on a 1-bit panel. Sizes are its bitmap strikes, in
# pixels; at 12 a character is 6 px wide.
FONT = "/usr/share/fonts/truetype/dotgothic16/DotGothic16-Regular.ttf"
BIG, TEXT = 32, 16  # pixels: the time, the other lines (DotGothic16's grid and twice it)
TEXT_CHARS = 18  # characters on a TEXT line left of the face (8 px each)
LABEL_FONT = "/usr/share/fonts/opentype/terminus/terminus-normal.otb"
LABEL_SIZE = 12  # pixels: the face's label, in Terminus to fit its column


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
    to_send: int = 0  # sessions waiting for wigle-sync (the mascot uploads)
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


def source_state(src: dict) -> str:
    """One Kismet datasource record as ok, ERR or off."""
    if src.get("kismet.datasource.error"):
        return "ERR"
    return "ok" if src.get("kismet.datasource.running") else "off"


def source_label(name: str) -> str:
    """Kismet's source names (kismet_site.conf: wlan_mon0, usb_bt) as shown."""
    return "bt" if "bt" in name.lower() or name.startswith("hci") else "wifi"


def gps_text(mode: int, satellites: int) -> str:
    """gpsd's fix as shown: "GPS 3D 9 SAT", "GPS NO FIX", or "GPS ?"."""
    if mode in (2, 3):
        return f"GPS {mode}D {satellites} SAT"
    if mode == 1:
        return "GPS NO FIX"
    return "GPS ?"


def source_errors(sources: dict[str, str]) -> list[str]:
    """Kismet's sources that aren't ok, BT first: "BT ERR", "WIFI OFF", or
    by band with two Wi-Fi radios (WIFI_SPLIT): "2G ERR". A radio missing
    from WIFI_BANDS shows by its name."""
    bt, wifi = [], {}
    for name, state in sorted(sources.items()):
        if source_label(name) == "bt":
            bt.append(f"BT {state.upper()}")
        else:
            wifi[name] = state
    if len(wifi) == 1:
        names = {name: "WIFI" for name in wifi}
    else:
        names = {name: WIFI_BANDS.get(name, name) for name in wifi}
    return [e for e in bt if not e.endswith(" OK")] + [
        f"{names[name]} {state.upper()}" for name, state in wifi.items() if state != "ok"
    ]


def errors(s: Status) -> list[str]:
    """What's wrong with what wardriving needs, the battery first. Kismet
    stopped at home is how it should be, so it isn't one."""
    found = []
    if s.battery_percent is not None and s.battery_percent <= LOW_BATTERY and not s.charging:
        found.append(f"BATT {s.battery_percent:.0f}%")
    if not s.kismet_up:
        if s.home is not True:
            found.append("KISMET DOWN")
    elif not s.sources:
        found.append("NO SOURCES")
    else:
        found += source_errors(s.sources)
    return found


def wrapped(words: list[str], width: int = TEXT_CHARS) -> list[str]:
    """Each item whole, as many to a line as fit in `width` characters."""
    lines: list[str] = []
    for word in words:
        if lines and len(lines[-1]) + 1 + len(word) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


# Emotions that take turns: five minutes each for the pairs, two for each
# upload step, so the panel isn't redrawn every poll for them.
LOOKING: tuple[Emotion, Emotion] = ("look_l", "look_r")
SLEEPING: tuple[Emotion, Emotion] = ("sleep", "sleep2")
UPLOADING: tuple[Emotion, Emotion, Emotion] = ("upload", "upload1", "upload2")


def emotion(s: Status) -> Emotion:
    """The mascot's emotion, from the worst thing on the screen up: looking
    left and right while it waits for a fix, sleep and sleep2 at home, the
    upload arrows stepping while sessions wait to go."""
    turn = s.now.minute // 5 % 2  # which of a pair, for five minutes
    if s.battery_percent is not None and s.battery_percent <= LOW_BATTERY and not s.charging:
        return "demotivated"
    if not s.kismet_up:
        if s.home is False:
            return "sad"
        if s.home and s.to_send:
            return UPLOADING[s.now.minute // 2 % len(UPLOADING)]
        return SLEEPING[turn]
    if not s.sources or any(state != "ok" for state in s.sources.values()):
        return "broken"
    if s.gps_mode in (2, 3):
        heard = sum((s.recent or {}).values())
        return "excited" if heard >= EXCITED else "cool"
    return LOOKING[turn]


def screen_lines(s: Status) -> list[str]:
    """The screen's text, top to bottom: the time (drawn larger), the GPS
    fix, then the errors or NO ERRORS. Pure, so the layout is tested without
    a panel."""
    clock = s.now.strftime("%H:%M") + ("" if s.clock_synced else "?")
    return [clock, gps_text(s.gps_mode, s.satellites), *(wrapped(errors(s)) or ["NO ERRORS"])]


def off_lines(reason: str | None, shutting_down: bool, now: datetime, clock_synced: bool) -> list[str]:
    """The last screen, left on the panel while the Pi is off. `reason` is
    pisugar-watch's (a low battery); otherwise the Pi is shutting down for
    some other reason, or only the display service stopped."""
    at = now.strftime("%Y-%m-%d %H:%M") + ("" if clock_synced else "?")
    if reason:
        return ["OFF", reason.upper(), at]
    if shutting_down:
        return ["OFF", "SHUT DOWN", at]
    return ["STOPPED", "DISPLAY STOPPED", "PI MAY BE ON", at]


def off_mood(reason: str | None, crashed: bool) -> Emotion:
    """The mascot left on the last screen. `reason` is pisugar-watch's, only
    ever a low battery."""
    if crashed:
        return "broken"
    return "demotivated" if reason else "sleep"


def error_lines(error: Exception, now: datetime, clock_synced: bool) -> list[str]:
    """The screen while a look at Kismet, gpsd or the battery fails."""
    at = now.strftime("%H:%M") + ("" if clock_synced else "?")
    return ["ERROR", type(error).__name__[:TEXT_CHARS], "SEE ITS JOURNAL", at]


def status_screen(get_status: Callable[[], Status], log) -> tuple[list[str], Emotion]:
    """The lines and the mascot's emotion for this poll: an ERROR screen (debug) when
    collecting fails, so the mascot stays and the loop keeps going."""
    try:
        status = get_status()
    except Exception as e:  # anything: the display must outlive it
        log.exception("collect failed")
        return error_lines(e, datetime.now(), chrony_synced()), "debug"
    return screen_lines(status), emotion(status)


@dataclass
class RefreshPolicy:
    """When to redraw: None (leave it), "partial" or "full"."""

    min_interval: float
    full_every: int
    full_interval: float
    shown: list[str] | None = None
    last: float = float("-inf")
    last_full: float = float("-inf")
    partials: int = 0

    def decide(self, lines: list[str], now: float) -> Refresh | None:
        if self.shown is None:
            return "full"
        if lines == self.shown or now - self.last < self.min_interval:
            return None
        if self.partials >= self.full_every or now - self.last_full >= self.full_interval:
            return "full"
        return "partial"

    def done(self, lines: list[str], kind: Refresh, now: float) -> None:
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
            tpv = sky = False
            for _ in range(40):
                line = f.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg.get("class") == "TPV":
                    mode = int(msg.get("mode", 0))
                    tpv = True
                elif msg.get("class") == "SKY":
                    sats = msg.get("satellites") or []
                    used = int(msg.get("uSat", sum(1 for x in sats if x.get("used"))))
                    sky = True
                if tpv and sky:
                    break
    except (OSError, ValueError):
        pass  # gpsd not running or garbled: shown as "GPS ?"
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


def to_send(capture: Path) -> int:
    """Sessions still waiting for wigle-sync, the one being written included."""
    try:
        return sum(1 for _ in capture.glob("*.wiglecsv"))
    except OSError:
        return 0


def run_ok(*cmd: str) -> bool | None:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None


def shutting_down() -> bool:
    """Whether systemd is stopping the whole system (not just this service)."""
    try:
        r = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.stdout.strip() == "stopping"


def collect(login: tuple[str, str] | None) -> Status:
    mode, sats = gps()
    sources, recent, kismet_up = {}, None, False
    if login:
        try:
            all_sources = kismet_get("/datasource/all_sources.json", login)
            devices = kismet_get(f"/devices/last-time/-{RECENT}/devices.json", login, ["kismet.device.base.phyname"])
        except (OSError, ValueError):
            pass  # Kismet stopped (at home) or still starting: shown as "Kismet stopped"
        else:
            sources = {
                src["kismet.datasource.name"]: source_state(src)
                for src in all_sources
                if src.get("kismet.datasource.name")
            }
            recent, kismet_up = count_recent(devices), True
    percent, charging = battery(STATE_DIR / "battery.json", time.time())
    return Status(
        home=run_ok("/usr/local/sbin/on-home-wifi"),
        gps_mode=mode,
        satellites=sats,
        kismet_up=kismet_up,
        sources=sources,
        recent=recent,
        battery_percent=percent,
        charging=charging,
        to_send=to_send(CAPTURE_DIR),
        clock_synced=chrony_synced(),
        now=datetime.now(),
    )


FACE_COLUMN = COLUMN  # px on the right for the face and its label
GAP = 7  # px from one line's baseline to the next line's capitals


def draw(lines: list[str], face: Emotion, rotate: int = 0):
    """The lines as a 1-bit landscape image for the panel, inside the corner
    chips: the first line at BIG, the rest at TEXT under it, and the face
    right of a divider with its emotion under it. Every screen has the face."""
    from PIL import Image
    from PIL import ImageDraw
    from PIL import ImageFont

    image = Image.new("1", (WIDTH, HEIGHT), 255)
    pen = ImageDraw.Draw(image)
    corner_chips(pen, WIDTH, HEIGHT)
    big, text = ImageFont.truetype(FONT, BIG), ImageFont.truetype(FONT, TEXT)
    left, right, top, bottom = BORDER + 1, WIDTH - 2 - BORDER, BORDER + 1, HEIGHT - 2 - BORDER
    fonts = [big] + [text] * (len(lines) - 1)
    caps = [-font.getbbox("0", anchor="ls")[1] for font in fonts]  # each line's cap height
    y = top + (bottom - top - sum(caps) - GAP * (len(lines) - 1)) // 2  # the block, centred
    for line, font, cap in zip(lines, fonts, caps):
        y += cap
        pen.text((left + 3, y), line, font=font, fill=0, anchor="ls")  # on its baseline
        y += GAP
    divider = right - FACE_COLUMN
    pen.line((divider, top, divider, bottom - 1), fill=0)
    middle = (divider + right) // 2 + 1
    label_top = bottom - LABEL_SIZE - 1
    draw_face(pen, face, middle, (top + label_top) // 2)
    pen.text((middle, label_top), label(face), font=ImageFont.truetype(LABEL_FONT, LABEL_SIZE), fill=0, anchor="ma")
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


def main() -> None:
    log = init_logging("eink-status")
    config = load_config()
    panel = Panel(config["model"])
    policy = RefreshPolicy(config["min_interval"], config["full_every"], config["full_interval"])
    creds = Path(os.environ.get("CREDENTIALS_DIRECTORY", "/nonexistent")) / "kismet-httpd"
    stop = stop_on_sigterm()
    log.info("started", model=config["model"], min_interval=config["min_interval"], full_every=config["full_every"])
    crashed, first = False, True
    try:
        while not stop.is_set():
            lines, face = status_screen(lambda: collect(credentials(creds)), log)
            # The mascot wakes up with the display: the first good screen is
            # awake (an ERROR screen keeps its debug, and the next one wakes).
            if first and face != "debug":
                face, first = "awake", False
            shown = [*lines, face]  # what a redraw compares
            now = time.monotonic()
            kind = policy.decide(shown, now)
            if kind:
                image = draw(lines, face, config["rotate"])
                if kind == "full":
                    panel.full(image)
                else:
                    panel.partial(image)
                policy.done(shown, kind, now)
            stop.wait(config["poll"])
    except BaseException:
        crashed = True
        raise
    finally:
        try:
            reason = (STATE_DIR / "shutdown-reason").read_text().strip()
        except OSError:
            reason = None
        last = off_lines(reason, shutting_down(), datetime.now(), chrony_synced())
        panel.full(draw(last, off_mood(reason, crashed), config["rotate"]))
        panel.sleep()
        log.info("stopped", reason=reason, crashed=crashed)


if __name__ == "__main__":
    sys.exit(main())
