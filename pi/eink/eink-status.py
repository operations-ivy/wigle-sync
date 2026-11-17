#!/usr/bin/env python3
"""The wardriver's status on its e-ink display (a Waveshare 2.13", as on a
pwnagotchi): what matters on the road, readable at a glance.

    +-[circuit-board border, a chip in each corner]-+
    | root@scootypuffsr:~#        (inverted bar)     |
    | $ away gps 3d 9 sat          |   [robot in     |
    | $ kismet BT ok wifi ok       |   sunglasses]   |
    | $ heard 245w 30bt            |                 |
    | $ sync 3h ago                |      cool       |
    | 14:32 2 to send_             |                 |
    +-----------------------------------------------+

- Home or away: on the home Wi-Fi or not (on-home-wifi, as pi-metrics uses).
- GPS: gpsd's fix (none, 2D, 3D) and the satellites in it.
- Kismet's capture sources: ok, ERR (in error), off; or "Kismet stopped"
  (at home it stops 15 minutes after joining the Wi-Fi).
- Devices Kismet heard in the last minute, by Wi-Fi and Bluetooth, rounded
  so the screen doesn't change on every count.
- The PiSugar's battery from pisugar-watch's state file ("+" while charging),
  left out while there's no reading (a board that isn't on I2C, as on
  scootypuffsr until it's re-seated), so it comes back by itself.
- The last sync: when wigle-sync last moved a session into uploaded/ (each
  file's ctime there; the Pi is home, with the real time, when that happens),
  "Nh ago" with the real time, the date and time without it.
- The time, with "?" until chrony has the real time, and how many sessions
  are waiting for wigle-sync (.wiglecsv files still in the capture directory).
- The look (the user, 2026-11-17): a root shell in Terminus, the Linux
  console's bitmap font, inside a circuit-board border (eink_art.py).
- The mascot on the right, an original sunglasses robot with one sprite for
  each of pwnagotchi's 25 face names (eink_art.py), picked by emotion():
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
from eink_art import circuit_border
from eink_art import COLUMN
from eink_art import draw_robot
from eink_art import Emotion
from eink_art import label
from handheld_common import chrony_synced
from handheld_common import init_logging
from handheld_common import stop_on_sigterm

Refresh = Literal["full", "partial"]

CONFIG = Path(os.environ.get("HANDHELD_CONFIG", "/etc/wigle-pi/handheld.yaml"))
STATE_DIR = Path(os.environ.get("WIGLE_PI_STATE", "/run/wigle-pi"))
KISMET = "http://127.0.0.1:2501"
# Where Kismet logs and wigle-sync collects (its PI_REMOTE_DIR, PI_ARCHIVE_DIR).
CAPTURE_DIR = Path(os.environ.get("KISMET_LOG_DIR", "/var/log/kismet"))
UPLOADED_DIR = CAPTURE_DIR / "uploaded"
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
FONT = "/usr/share/fonts/opentype/terminus/terminus-normal.otb"
FONT_BOLD = "/usr/share/fonts/opentype/terminus/terminus-bold.otb"
TITLE_SIZE, BODY_SIZE = 12, 12  # pixels: the title bar, everything else


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
    last_sync: float | None = None  # epoch seconds
    to_send: int = 0  # sessions waiting for wigle-sync
    clock_synced: bool = False
    now: datetime = field(default_factory=datetime.now)
    host: str = "wigle"  # the prompt's hostname


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


def rounded(n: int) -> str:
    """Counts as shown: exact under 20, to 5 under 100, to 10 under 1000,
    then "1.2k", so a busy street doesn't redraw the screen on every poll."""
    if n < 20:
        return str(n)
    if n >= 995:
        return f"{n / 1000:.1f}k"
    step = 5 if n < 100 else 10
    return str(round(n / step) * step)


def source_state(src: dict) -> str:
    """One Kismet datasource record as ok, ERR or off."""
    if src.get("kismet.datasource.error"):
        return "ERR"
    return "ok" if src.get("kismet.datasource.running") else "off"


def source_label(name: str) -> str:
    """Kismet's source names (kismet_site.conf: wlan_mon0, usb_bt) as shown."""
    return "bt" if "bt" in name.lower() or name.startswith("hci") else "wifi"


def gps_text(mode: int, satellites: int) -> str:
    """gpsd's fix as shown: "gps 3d 9 sat", "gps no fix", or "gps ?"."""
    if mode in (2, 3):
        return f"gps {mode}d {satellites} sat"
    if mode == 1:
        return "gps no fix"
    return "gps ?"


def ago(seconds: float) -> str:
    """A coarse age that changes at most hourly: "<1h", "5h", "3d"."""
    hours = max(0, int(seconds // 3600))
    if hours < 1:
        return "<1h"
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def sync_text(last: float | None, now: datetime, clock_synced: bool) -> str:
    """The last sync as shown: "sync 3h ago", "sync never". Without
    the real time "ago" can't be trusted, so it's the sync's own date and time
    (taken at home, with the real time)."""
    if last is None:
        return "sync never"
    if clock_synced:
        return f"sync {ago(now.timestamp() - last)} ago"
    return "sync " + datetime.fromtimestamp(last).strftime("%m-%d %H:%M")


# Emotions that take turns: five minutes each for the pairs, two for each
# upload step, so the panel isn't redrawn every poll for them.
LOOKING: tuple[Emotion, Emotion] = ("look_l", "look_r")
SLEEPING: tuple[Emotion, Emotion] = ("sleep", "sleep2")
UPLOADING: tuple[Emotion, Emotion, Emotion] = ("upload", "upload1", "upload2")


def emotion(s: Status) -> Emotion:
    """The robot's emotion, from the worst thing on the screen up: looking
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
    """The screen's text, top to bottom: the first line is the title bar (a
    root prompt), the rest shell lines, the last ending in a cursor. Pure, so
    the layout is tested without a panel."""
    where = {True: "home", False: "away", None: "?"}[s.home]
    if not s.kismet_up:
        sources = "$ kismet stopped"
    elif not s.sources:
        sources = "$ kismet: no sources"
    else:
        sources = "$ kismet " + " ".join(f"{source_label(n)} {state}" for n, state in sorted(s.sources.items()))
    if s.recent is None:
        heard = "$ heard -"
    else:
        heard = f"$ heard {rounded(s.recent.get('wifi', 0))}w {rounded(s.recent.get('bluetooth', 0))}bt"
    lines = [f"root@{s.host}:~#", f"$ {where} {gps_text(s.gps_mode, s.satellites)}", sources, heard]
    if s.battery_percent is not None:  # left out without a reading
        lines.append(f"$ batt {s.battery_percent:.0f}%" + (" +" if s.charging else ""))
    clock = s.now.strftime("%H:%M") + ("" if s.clock_synced else " ?")
    if s.to_send:
        clock += f" {s.to_send} to send"
    return [*lines, f"$ {sync_text(s.last_sync, s.now, s.clock_synced)}", clock + "_"]


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


def off_mood(reason: str | None, crashed: bool) -> Emotion:
    """The robot left on the last screen. `reason` is pisugar-watch's, only
    ever a low battery."""
    if crashed:
        return "broken"
    return "demotivated" if reason else "sleep"


def error_lines(error: Exception, now: datetime, clock_synced: bool) -> list[str]:
    """The screen while a look at Kismet, gpsd or the battery fails."""
    at = now.strftime("%H:%M") + ("" if clock_synced else " ?")
    return ["ERROR", type(error).__name__[:20], "see its journal", "", at]


def status_screen(get_status: Callable[[], Status], log) -> tuple[list[str], Emotion]:
    """The lines and the robot's emotion for this poll: an ERROR screen (debug) when
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


def last_sync(uploaded: Path) -> float | None:
    """When wigle-sync last moved a file into uploaded/: the newest ctime there
    (a rename sets it; pruning old sessions doesn't touch the rest)."""
    try:
        return max((f.stat().st_ctime for f in uploaded.iterdir() if f.is_file()), default=None)
    except OSError:
        return None


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
        last_sync=last_sync(UPLOADED_DIR),
        to_send=to_send(CAPTURE_DIR),
        clock_synced=chrony_synced(),
        now=datetime.now(),
        host=socket.gethostname(),
    )


FACE_COLUMN = COLUMN  # px on the right for the robot and its label
TITLE_BAR = 15  # px: the inverted title bar's height
ROW = 15  # px between shell lines, when they fit (less with a battery line)


def draw(lines: list[str], face: Emotion, rotate: int = 0):
    """The lines as a 1-bit landscape image for the panel, inside the circuit
    border: the first line in an inverted title bar, the rest below it, and
    the robot right of a divider with its emotion under it. Every screen has
    the robot."""
    from PIL import Image
    from PIL import ImageDraw
    from PIL import ImageFont

    image = Image.new("1", (WIDTH, HEIGHT), 255)
    pen = ImageDraw.Draw(image)
    circuit_border(pen, WIDTH, HEIGHT)
    title, body = ImageFont.truetype(FONT_BOLD, TITLE_SIZE), ImageFont.truetype(FONT, BODY_SIZE)
    left, right, top, bottom = BORDER + 1, WIDTH - 2 - BORDER, BORDER + 1, HEIGHT - 2 - BORDER
    pen.rectangle((left, top, right, top + TITLE_BAR - 1), fill=0)
    if lines:
        pen.text((left + 3, top + 1), lines[0], font=title, fill=255)
    rows, below = lines[1:], top + TITLE_BAR + 3  # the shell lines start under the bar
    step = min(ROW, (bottom - below) // max(1, len(rows)))
    for i, line in enumerate(rows):
        pen.text((left + 5, below + i * step), line, font=body, fill=0)
    divider = right - FACE_COLUMN
    pen.line((divider, below, divider, bottom - 1), fill=0)
    middle = (divider + right) // 2 + 1
    draw_robot(pen, face, middle, below + 1)
    pen.text((middle, bottom - BODY_SIZE - 1), label(face), font=body, fill=0, anchor="ma")
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
            # The robot wakes up with the display: the first good screen is
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
