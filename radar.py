"""The wardriving radar: where recent drives found networks, around home.

Data comes from WiGLE, not the Pi: each finished upload has a KML of the
networks it located (/file/kml/<transid>). An upload never changes once
processed, so each KML is fetched once and kept, and only the newest
RADAR_UPLOADS uploads are kept at all. Only position, time and type survive
parsing: no network names or IDs leave this module.

The centre is the median of all contacts (drives start and end at home), or
RADAR_CENTER ("lat,lon", from the wigle-sync Secret) when set.
"""

from __future__ import annotations

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
# Ranges the scope snaps to, so the rings read as round numbers.
NICE_RADII_M = [500, 1000, 2000, 3000, 5000, 10000, 20000, 50000, 100000]
EARTH_M = 6371000

_PLACEMARK = re.compile(r"<Placemark>(.*?)</Placemark>", re.S)
_COORDS = re.compile(r"<coordinates>\s*([-\d.]+),([-\d.]+)")
_TIME = re.compile(r"Time:\s*(?:</b>)?\s*([0-9T:.+-]+)")
_TYPE = re.compile(r"Type:\s*(?:</b>)?\s*([A-Za-z]+)")


def parse_kml(text: str) -> list[tuple[float, float, float, str]]:
    """(lat, lon, seen epoch seconds, type) for each located network."""
    out = []
    for body in _PLACEMARK.findall(text):
        coords = _COORDS.search(body)
        if not coords:
            continue
        desc = html.unescape(body)
        seen = _TIME.search(desc)
        kind = _TYPE.search(desc)
        try:
            lon, lat = float(coords.group(1)), float(coords.group(2))
            when = datetime.fromisoformat(seen.group(1)).timestamp() if seen else 0.0
        except ValueError:
            continue
        out.append((round(lat, 5), round(lon, 5), when, (kind.group(1) if kind else "wifi").lower()))
    return out


def parse_center(text: str) -> tuple[float, float] | None:
    try:
        lat, lon = (float(v) for v in text.split(","))
    except ValueError:
        return None
    return (lat, lon) if -90 <= lat <= 90 and -180 <= lon <= 180 else None


def project(points, center, now: float) -> dict[str, Any]:
    """Bearing (degrees from north), range (0..1 of the radius) and age for
    each point, with a radius that holds 90% of them."""
    if not points:
        return {"radius_m": NICE_RADII_M[1], "contacts": []}
    lat0, lon0 = map(math.radians, center)
    polar = []
    for lat, lon, seen, kind in points:
        la, lo = math.radians(lat), math.radians(lon)
        x = (lo - lon0) * math.cos((la + lat0) / 2) * EARTH_M
        y = (la - lat0) * EARTH_M
        polar.append((math.degrees(math.atan2(x, y)) % 360, math.hypot(x, y), seen, kind))
    dists = sorted(p[1] for p in polar)
    p90 = dists[int(0.9 * (len(dists) - 1))]
    radius = next((r for r in NICE_RADII_M if r >= p90), NICE_RADII_M[-1])
    contacts = [[round(b, 1), round(d / radius, 3), max(0, round(now - s)) if s else None, k]
                for b, d, s, k in polar if d <= radius]
    return {"radius_m": radius, "contacts": contacts}


class Radar:
    def __init__(self, auth: tuple[str, str], center: tuple[float, float] | None = None) -> None:
        self.auth = auth
        self.center = center
        self._lock = threading.Lock()
        self._kml: dict[str, list] = {}  # transid -> parsed points, newest uploads only

    def _get(self, path: str, **params) -> requests.Response:
        r = requests.get(f"{WIGLE_API_URL}{path}", params=params, auth=self.auth,
                         headers={"Accept": "application/json"}, timeout=60)
        r.raise_for_status()
        return r

    def refresh(self) -> None:
        uploads = self._get("/file/transactions", pagestart=0, pageend=100).json().get("results", [])
        done = [u["transid"] for u in uploads if u.get("status") == "D" and (u.get("totalGps") or 0) > 0]
        keep = done[:RADAR_UPLOADS]
        for transid in keep:
            if transid in self._kml:
                continue
            points = parse_kml(self._get(f"/file/kml/{transid}").text)
            with self._lock:
                self._kml[transid] = points
            log.info("Radar loaded an upload", transid=transid, contacts=len(points))
        with self._lock:
            for transid in list(self._kml):
                if transid not in keep:
                    del self._kml[transid]

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        with self._lock:
            points = [p for pts in self._kml.values() for p in pts]
            drives = len(self._kml)
        if not points:
            return {"radius_m": NICE_RADII_M[1], "contacts": [], "drives": 0, "total": 0, "centered": "none"}
        center = self.center or (statistics.median(p[0] for p in points), statistics.median(p[1] for p in points))
        return {**project(points, center, now), "drives": drives, "total": len(points),
                "centered": "home" if self.center else "median"}
