"""Data for the drive replay and the console's Almanac: where and when recent
drives found networks.

Data comes from WiGLE, not the Pi: each finished upload has a KML of the
networks it located (/file/kml/<transid>). An upload never changes once
processed, so each KML is fetched once and kept.

- The newest RADAR_UPLOADS uploads ("drives") are kept in full: position,
  time and type for each network, plus whether this is the first drive (of
  those held here) that saw it.
- Up to GHOST_UPLOADS older ones are kept only as coarse positions, for the
  faint streets under the replay: networks line the roads, so years of
  drives draw the street grid without a map. They're fetched a few
  per refresh, to go easy on WiGLE's API.

Network names never leave parse_kml. Network IDs are only used to tell repeat
sightings from first ones, as an in-memory hash, and never leave this module.
Positions leave as metres from the centre, never as coordinates.

The centre is the median of the recent contacts (drives start and end at
home), or RADAR_CENTER ("lat,lon", from the wigle-sync Secret) when set.

The page's JSON is built once per refresh and kept gzipped: building it on
request OOM-killed a 192Mi pod (2026-10-30).
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import math
import os
import re
import statistics
import threading
import time
from datetime import datetime
from typing import Any

import requests
import brick_observe

import console_metrics
from wigle_client import WIGLE_API_URL

log = brick_observe.get_logger(__name__)

RADAR_UPLOADS = int(os.environ.get("RADAR_UPLOADS", "12"))
GHOST_UPLOADS = int(os.environ.get("GHOST_UPLOADS", "60"))
# New KMLs fetched per refresh beyond the recent drives, so filling the ghost
# layer takes a few refreshes instead of one burst against WiGLE.
FETCHES_PER_REFRESH = int(os.environ.get("RADAR_FETCHES_PER_REFRESH", "5"))
# The page's first view holds this share of the contacts.
VIEW_SHARE = 0.95
# The ghost layer is quantised to cells this wide.
GHOST_CELL_M = 25
# A gap this long in a drive's timestamps is a clock step, not a stop: brick69
# has no RTC and has started days behind (wigle-sync#44 fixed that for new
# drives). The drive's time is its largest unbroken run.
CLOCK_GAP_S = 6 * 3600
KINDS = ("wifi", "ble", "bt")
EARTH_M = 6371000

_PLACEMARK = re.compile(r"<Placemark>(.*?)</Placemark>", re.S)
_COORDS = re.compile(r"<coordinates>\s*([-\d.]+),([-\d.]+)")
_TIME = re.compile(r"Time:\s*(?:</b>)?\s*([0-9T:.+-]+)")
_TYPE = re.compile(r"Type:\s*(?:</b>)?\s*([A-Za-z]+)")
_NETID = re.compile(r"Network ID:\s*(?:<b>)?\s*([^<\s]+)")
_TRANSID_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})-")


def _key(netid: str) -> int:
    """A network's identity for spotting repeats, without keeping the ID."""
    return int.from_bytes(hashlib.blake2b(netid.lower().encode(), digest_size=8).digest(), "big")


def parse_kml(text: str) -> list[tuple[float, float, float, str, int]]:
    """(lat, lon, seen epoch seconds, type, key) for each located network."""
    out = []
    for body in _PLACEMARK.findall(text):
        coords = _COORDS.search(body)
        if not coords:
            continue
        desc = html.unescape(body)
        seen, kind, netid = _TIME.search(desc), _TYPE.search(desc), _NETID.search(desc)
        try:
            lon, lat = float(coords.group(1)), float(coords.group(2))
            when = datetime.fromisoformat(seen.group(1)).timestamp() if seen else 0.0
        except ValueError:
            continue
        key = _key(netid.group(1)) if netid else _key(f"{lat:.5f},{lon:.5f}")
        out.append((round(lat, 5), round(lon, 5), when, (kind.group(1) if kind else "wifi").lower(), key))
    return out


def parse_center(text: str) -> tuple[float, float] | None:
    try:
        lat, lon = (float(v) for v in text.split(","))
    except ValueError:
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def drive_date(transid: str) -> str:
    """WiGLE's transids start with the upload date: 20261009-01234 -> 2026-10-09."""
    m = _TRANSID_DATE.match(transid)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _metres(lat: float, lon: float, lat0: float, lon0: float) -> tuple[int, int]:
    """East and north of the centre (radians), in whole metres."""
    la, lo = math.radians(lat), math.radians(lon)
    return round((lo - lon0) * math.cos((la + lat0) / 2) * EARTH_M), round((la - lat0) * EARTH_M)


def main_run(times: list[float]) -> tuple[float, float]:
    """The first and last of the largest run of sorted times with no clock gap."""
    if not times:
        return 0.0, 0.0
    runs, first = [], 0
    for i in range(1, len(times)):
        if times[i] - times[i - 1] > CLOCK_GAP_S:
            runs.append((i - first, first, i - 1))
            first = i
    runs.append((len(times) - first, first, len(times) - 1))
    _, a, b = max(runs)
    return times[a], times[b]


def drive_columns(points, firsts: set[int], center: tuple[float, float]) -> tuple[dict[str, Any], dict[str, list]]:
    """One drive's summary and its points as columns: x, y (metres from the
    centre), t (seconds from the drive's start), k (index into KINDS), n (new)."""
    lat0, lon0 = map(math.radians, center)
    start, end = main_run(sorted(p[2] for p in points if p[2]))
    cols: dict[str, list] = {"x": [], "y": [], "t": [], "k": [], "n": []}
    kinds = dict.fromkeys(KINDS, 0)
    # Clamped times included, so the page can bisect on t.
    for lat, lon, seen, kind, key in sorted(points, key=lambda p: min(max(p[2], start), end)):
        x, y = _metres(lat, lon, lat0, lon0)
        kind = kind if kind in kinds else "wifi"
        kinds[kind] += 1
        cols["x"].append(x)
        cols["y"].append(y)
        cols["t"].append(round(min(max(0.0, seen - start), end - start)) if seen else 0)
        cols["k"].append(KINDS.index(kind))
        cols["n"].append(int(key in firsts))
    return {"start": round(start), "end": round(end), "kinds": kinds}, cols


def ghost_cells(cells, center: tuple[float, float]) -> list[int]:
    """Older drives' positions as unique cells, flat: [x, y, x, y, ...] metres."""
    lat0, lon0 = map(math.radians, center)
    seen = set()
    for lat, lon in cells:
        x, y = _metres(lat, lon, lat0, lon0)
        seen.add((round(x / GHOST_CELL_M) * GHOST_CELL_M, round(y / GHOST_CELL_M) * GHOST_CELL_M))
    return [v for cell in sorted(seen) for v in cell]


def empty_page() -> dict[str, Any]:
    return {
        "at": 0,
        "centered": "none",
        "view_m": 1000,
        "kinds": KINDS,
        "drives": [],
        "points": [],
        "ghost": [],
        "ghost_drives": 0,
    }


class Radar:
    def __init__(self, auth: tuple[str, str], center: tuple[float, float] | None = None) -> None:
        self.auth = auth
        self.center = center
        self._lock = threading.Lock()
        self._recent: dict[str, list] = {}  # transid -> parse_kml points, newest drives
        self._ghost: dict[str, set] = {}  # transid -> {(lat, lon) at ~11 m}, older drives
        self._keys: dict[str, set] = {}  # transid -> network keys, for first sightings
        self._order: list[str] = []  # every kept transid, newest first
        self._page = gzip.compress(json.dumps(empty_page()).encode())  # gzipped JSON, see build_page()
        # Set once the first refresh has finished, whether or not WiGLE
        # answered: the pod's readiness waits for it (console.py /ready).
        self.first_load_done = threading.Event()

    def _get(self, path: str, accept: str = "application/json", **params) -> requests.Response:
        # The KML endpoint answers 406 to "Accept: application/json".
        r = requests.get(
            f"{WIGLE_API_URL}{path}", params=params, auth=self.auth, headers={"Accept": accept}, timeout=60
        )
        r.raise_for_status()
        return r

    def refresh(self) -> None:
        uploads = self._get("/file/transactions", pagestart=0, pageend=100).json().get("results", [])
        done = [u["transid"] for u in uploads if u.get("status") == "D" and (u.get("totalGps") or 0) > 0]
        recent, older = done[:RADAR_UPLOADS], done[RADAR_UPLOADS : RADAR_UPLOADS + GHOST_UPLOADS]
        budget = FETCHES_PER_REFRESH
        try:
            for transid in recent + older:
                if transid in self._recent or transid in self._ghost:
                    continue
                if transid not in recent:
                    if budget <= 0:
                        continue
                    budget -= 1
                points = parse_kml(self._get(f"/file/kml/{transid}", accept="*/*").text)
                console_metrics.KML_FETCHES.inc()
                with self._lock:
                    self._keys[transid] = {p[4] for p in points}
                    if transid in recent:
                        self._recent[transid] = points
                    else:
                        self._ghost[transid] = {(round(p[0], 4), round(p[1], 4)) for p in points}
                log.info(
                    "Radar loaded an upload",
                    transid=transid,
                    contacts=len(points),
                    layer="recent" if transid in recent else "ghost",
                )
        finally:
            # Even when a fetch fails partway, show what did load: otherwise a
            # pod holds every recent drive and shows none until the next refresh.
            self._settle(done, recent, older)

    def _settle(self, done: list[str], recent: list[str], older: list[str]) -> None:
        with self._lock:
            # A drive that has aged out of the recent set drops to the ghost layer.
            for transid in [t for t in self._recent if t not in recent]:
                self._ghost[transid] = {(round(p[0], 4), round(p[1], 4)) for p in self._recent.pop(transid)}
            for store in (self._recent, self._ghost, self._keys):
                for transid in [t for t in store if t not in recent and t not in older]:
                    del store[transid]
            self._order = [t for t in done if t in self._recent or t in self._ghost]

    def refresh_safely(self) -> None:
        """refresh(), logging instead of raising (WiGLE down or rate limited
        keeps what's already loaded), and marking the first attempt done."""
        started = time.monotonic()
        try:
            self.refresh()
            console_metrics.RADAR_REFRESHES.labels("ok").inc()
            console_metrics.RADAR_LAST_SUCCESS.set_to_current_time()
        except Exception as e:
            log.warning("Radar refresh failed", error=str(e))
            console_metrics.RADAR_REFRESHES.labels("error").inc()
        finally:
            # Whatever did load before a failure still reaches the page.
            try:
                self.publish()
            except Exception as e:
                log.warning("Radar page build failed", error=str(e))
            console_metrics.RADAR_REFRESH_SECONDS.observe(time.monotonic() - started)
            self.first_load_done.set()

    def counts(self) -> dict[str, int]:
        """How many drives each layer holds, and the recent drives' contacts."""
        with self._lock:
            return {
                "recent": len(self._recent),
                "ghost": len(self._ghost),
                "contacts": sum(len(p) for p in self._recent.values()),
            }

    def build_page(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            order = [t for t in self._order if t in self._recent]
            recent = {t: self._recent[t] for t in order}
            cells = {c for layer in self._ghost.values() for c in layer}
            ghost_drives = len(self._ghost)
            # Oldest first, so a network counts as new on the first drive that saw it.
            known: set[int] = set()
            for t in reversed(self._order):
                if t not in self._recent:
                    known |= self._keys.get(t, set())
            firsts: dict[str, set] = {}
            for t in reversed(order):
                keys = self._keys.get(t, set())
                firsts[t] = keys - known
                known |= keys
        if not recent:
            return empty_page()
        points = [p for t in order for p in recent[t]]
        center = self.center or (statistics.median(p[0] for p in points), statistics.median(p[1] for p in points))
        drives, columns = [], []
        for t in order:
            summary, cols = drive_columns(recent[t], firsts[t], center)
            drives.append({"date": drive_date(t), "contacts": len(recent[t]), "new": len(firsts[t]), **summary})
            columns.append(cols)
        lat0, lon0 = map(math.radians, center)
        dists = sorted(math.hypot(*_metres(p[0], p[1], lat0, lon0)) for p in points)
        return {
            "at": round(now),
            "centered": "home" if self.center else "median",
            "view_m": round(dists[int(VIEW_SHARE * (len(dists) - 1))]),
            "kinds": KINDS,
            "drives": drives,
            "points": columns,
            "ghost": ghost_cells(cells, center),
            "ghost_drives": ghost_drives,
        }

    def publish(self) -> None:
        page = gzip.compress(json.dumps(self.build_page(), separators=(",", ":")).encode())
        self._page = page

    def page_gz(self) -> bytes:
        return self._page
