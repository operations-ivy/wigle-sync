"""The wardriving radar: where recent drives found networks, around home.

Data comes from WiGLE, not the Pi: each finished upload has a KML of the
networks it located (/file/kml/<transid>). An upload never changes once
processed, so each KML is fetched once and kept.

- The newest RADAR_UPLOADS uploads ("drives") are kept in full: position,
  time and type for each network, plus whether this is the first drive (of
  those the radar holds) that saw it.
- Up to GHOST_UPLOADS older ones are kept only as coarse positions, for the
  faint "ghost streets" under the scope: networks line the roads, so years of
  drives draw the street grid without a map. They're fetched a few per
  refresh, to go easy on WiGLE's API.

Network names never leave parse_kml. Network IDs are only used to tell repeat
sightings from first ones, as an in-memory hash, and never leave this module.

The centre is the median of the recent contacts (drives start and end at
home), or RADAR_CENTER ("lat,lon", from the wigle-sync Secret) when set.
"""

from __future__ import annotations

import hashlib
import html
import math
import os
import re
import statistics
import threading
import time
from datetime import datetime
from typing import Any

import requests
import structlog

from wigle_client import WIGLE_API_URL

log = structlog.get_logger()

RADAR_UPLOADS = int(os.environ.get("RADAR_UPLOADS", "12"))
GHOST_UPLOADS = int(os.environ.get("GHOST_UPLOADS", "60"))
# New KMLs fetched per refresh beyond the recent drives, so filling the ghost
# layer takes a few refreshes instead of one burst against WiGLE.
FETCHES_PER_REFRESH = int(os.environ.get("RADAR_FETCHES_PER_REFRESH", "5"))
# Ranges the scope snaps to, so the rings read as round numbers.
NICE_RADII_M = [500, 1000, 2000, 3000, 5000, 10000, 20000, 50000, 100000]
# The ghost layer is quantised to this many cells across the scope's radius.
GHOST_CELLS = 300
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


def _polar(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    la, lo = math.radians(lat), math.radians(lon)
    x = (lo - lon0) * math.cos((la + lat0) / 2) * EARTH_M
    y = (la - lat0) * EARTH_M
    return math.degrees(math.atan2(x, y)) % 360, math.hypot(x, y)


def project(points, center, now: float) -> dict[str, Any]:
    """Bearing (degrees from north), range (0..1 of the radius), age, type,
    drive and first-seen flag for each point, with a radius that holds 90%
    of them. Points are (lat, lon, seen, kind, drive, new)."""
    if not points:
        return {"radius_m": NICE_RADII_M[1], "contacts": []}
    lat0, lon0 = map(math.radians, center)
    polar = [(*_polar(p[0], p[1], lat0, lon0), *p[2:]) for p in points]
    dists = sorted(p[1] for p in polar)
    p90 = dists[int(0.9 * (len(dists) - 1))]
    radius = next((r for r in NICE_RADII_M if r >= p90), NICE_RADII_M[-1])
    contacts = [[round(b, 1), round(d / radius, 3), max(0, round(now - s)) if s else None, k, drive, int(new)]
                for b, d, s, k, drive, new in polar if d <= radius]
    return {"radius_m": radius, "contacts": contacts}


def ghost(cells, center, radius_m: float) -> list[list[float]]:
    """The older drives' positions as unique scope cells: [bearing, range]."""
    lat0, lon0 = map(math.radians, center)
    seen = set()
    for lat, lon in cells:
        b, d = _polar(lat, lon, lat0, lon0)
        if d > radius_m:
            continue
        r = d / radius_m
        # Cells of roughly equal size: finer in angle further out.
        ring = round(r * GHOST_CELLS)
        step = 360 / max(6, round(2 * math.pi * ring))
        seen.add((round(round(b / step) * step, 2) % 360, round(ring / GHOST_CELLS, 4)))
    return [list(c) for c in sorted(seen)]


class Radar:
    def __init__(self, auth: tuple[str, str], center: tuple[float, float] | None = None) -> None:
        self.auth = auth
        self.center = center
        self._lock = threading.Lock()
        self._recent: dict[str, list] = {}   # transid -> parse_kml points, newest drives
        self._ghost: dict[str, set] = {}     # transid -> {(lat, lon) at ~11 m}, older drives
        self._keys: dict[str, set] = {}      # transid -> network keys, for first sightings
        self._order: list[str] = []          # every kept transid, newest first
        # snapshot() is the costly part (every ghost cell projected), and the
        # data only changes on refresh: recompute when the order changes.
        self._snap: tuple[tuple, dict] | None = None
        # Set once the first refresh has finished, whether or not WiGLE
        # answered: the pod's readiness waits for it (console.py /ready).
        self.first_load_done = threading.Event()

    def _get(self, path: str, accept: str = "application/json", **params) -> requests.Response:
        # The KML endpoint answers 406 to "Accept: application/json".
        r = requests.get(f"{WIGLE_API_URL}{path}", params=params, auth=self.auth,
                         headers={"Accept": accept}, timeout=60)
        r.raise_for_status()
        return r

    def refresh(self) -> None:
        uploads = self._get("/file/transactions", pagestart=0, pageend=100).json().get("results", [])
        done = [u["transid"] for u in uploads if u.get("status") == "D" and (u.get("totalGps") or 0) > 0]
        recent, older = done[:RADAR_UPLOADS], done[RADAR_UPLOADS:RADAR_UPLOADS + GHOST_UPLOADS]
        budget = FETCHES_PER_REFRESH
        for transid in recent + older:
            if transid in self._recent or transid in self._ghost:
                continue
            if transid not in recent:
                if budget <= 0:
                    continue
                budget -= 1
            points = parse_kml(self._get(f"/file/kml/{transid}", accept="*/*").text)
            with self._lock:
                self._keys[transid] = {p[4] for p in points}
                if transid in recent:
                    self._recent[transid] = points
                else:
                    self._ghost[transid] = {(round(p[0], 4), round(p[1], 4)) for p in points}
            log.info("Radar loaded an upload", transid=transid, contacts=len(points),
                     layer="recent" if transid in recent else "ghost")
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
        try:
            self.refresh()
        except Exception as e:
            log.warning("Radar refresh failed", error=str(e))
        finally:
            self.first_load_done.set()

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            version = (tuple(self._order), len(self._ghost))
            if self._snap and self._snap[0] == version and now - self._snap[1].get("at", 0) < 300:
                return self._snap[1]
            order = [t for t in self._order if t in self._recent]
            recent = {t: self._recent[t] for t in order}
            ghost_cells = {c for cells in self._ghost.values() for c in cells}
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
        empty = {"radius_m": NICE_RADII_M[1], "contacts": [], "drives": [], "ghost": [], "total": 0,
                 "ghost_drives": 0, "centered": "none"}
        if not recent:
            return empty
        points = [(p[0], p[1], p[2], p[3], i, p[4] in firsts[t])
                  for i, t in enumerate(order) for p in recent[t]]
        center = self.center or (statistics.median(p[0] for p in points), statistics.median(p[1] for p in points))
        projected = project(points, center, now)
        drives = [{"date": drive_date(t), "contacts": len(recent[t]), "new": len(firsts[t])} for t in order]
        snap = {**projected, "drives": drives, "total": len(points),
                "ghost": ghost(ghost_cells, center, projected["radius_m"]),
                "ghost_drives": len(self._ghost), "centered": "home" if self.center else "median", "at": now}
        with self._lock:
            self._snap = (version, snap)
        return snap
