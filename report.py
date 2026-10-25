"""The after-action report: what one drive's .wiglecsv shows, for the console.

The sync builds it while it has the file (the console never sees the CSV) and
logs it as one structured line; the console reads the newest from Loki. One
report per file: two Kismet sessions on one outing are two reports.

What the CSV supports, and what it doesn't:
- Kismet writes one row per sighting, with the time (UTC) and the GPS fix.
- LED2 (pi/leds/status-leds.py) lights while a Flock or Axon device was heard
  in the last 60 s, so its on-time is rebuilt from those devices' rows.
- RSSI 0 means Kismet had no reading (every Bluetooth row so far), so it's
  skipped.
- AccuracyMeters is always 0 and AuthMode only ever [WPS][ESS] or [ESS]:
  neither is reported.
- Stretches with no rows at all are listed as gaps: on 2026-10-24 one Kismet
  session wrote nothing for 99 minutes, most likely while the GPS had no fix.
- Names have their non-ASCII bytes as octal escapes, decoded here.
- Power pulled at the end of a drive leaves a cut-off last row and NUL
  padding; both are dropped and counted (partial_rows, nul_bytes).

Names, places and times are in it: the user's call (2026-10-25). It goes to
Loki, never to git."""

from __future__ import annotations

import csv
import io
import math
import re
from collections import Counter
from datetime import datetime
from datetime import timezone
from pathlib import Path

import detect

# status-leds.py: ALERT_KINDS and WINDOW.
LED2_KINDS = {"flock", "axon"}
LED2_WINDOW = 60
TYPES = {"WIFI": "wifi", "BLE": "ble", "BT": "bt"}
RSSI_BINS = list(range(-110, -10, 10))  # each bin's lower edge, dBm
SIGHTING_BUCKETS = ((1, 1), (2, 2), (3, 5), (6, 10), (11, 50), (51, None))
TOP_NETWORKS = 10
MAX_DETECTIONS = 50
# A GPS jump faster than this between two rows is a bad fix, not driving.
MAX_SPEED_MPS = 70
# No rows for this long is worth a note (Kismet seems to write none without a GPS fix).
GAP_SECONDS = 300
# Kismet writes a name's non-ASCII bytes as octal escapes: C\342\200\231s is C’s.
_OCTAL = re.compile(rb"(?:\\[0-7]{3})+")


def _name(value: str | None) -> str:
    raw = (value or "").strip().encode("utf-8", "surrogateescape")
    return _OCTAL.sub(lambda m: bytes(int(o, 8) for o in m.group().split(b"\\")[1:]), raw).decode("utf-8", "replace")


def _epoch(value: str) -> float | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def _rows(text: str) -> tuple[list[dict], int]:
    """The CSV's whole rows, and how many partial ones were dropped."""
    lines = text.replace("\0", "").splitlines()
    rows, partial = [], 0
    for row in csv.DictReader(io.StringIO("\n".join(lines[1:]))):
        if row.get("Type") not in TYPES or _epoch(row.get("FirstSeen")) is None:
            partial += 1
            continue
        row["_t"] = _epoch(row["FirstSeen"])
        rows.append(row)
    return rows, partial


def _lit_seconds(times: list[float]) -> list[tuple[float, float]]:
    """LED2's on-spans: each sighting keeps it lit for LED2_WINDOW seconds."""
    spans: list[tuple[float, float]] = []
    for t in sorted(times):
        if spans and t <= spans[-1][1]:
            spans[-1] = (spans[-1][0], t + LED2_WINDOW)
        else:
            spans.append((t, t + LED2_WINDOW))
    return spans


def _sighting_buckets(counts: Counter) -> list[dict]:
    out = []
    for lo, hi in SIGHTING_BUCKETS:
        n = sum(1 for c in counts.values() if c >= lo and (hi is None or c <= hi))
        out.append({"label": f"{lo}" if lo == hi else f"{lo}+" if hi is None else f"{lo}-{hi}", "devices": n})
    return out


def build_text(name: str, text: str) -> dict | None:
    """The report for a .wiglecsv's text, or None when it has no whole rows."""
    rows, partial = _rows(text)
    if not rows:
        return None
    rows.sort(key=lambda r: r["_t"])
    start, end = rows[0]["_t"], rows[-1]["_t"]
    first_minute = int(start // 60 * 60)
    n_minutes = int((end - first_minute) // 60) + 1

    per_minute = {t: [0] * n_minutes for t in TYPES.values()}
    new_per_minute = {t: [0] * n_minutes for t in TYPES.values()}
    sightings = {t: Counter() for t in TYPES.values()}
    rssi = {t: [0] * len(RSSI_BINS) for t in TYPES.values()}
    channels: dict[str, Counter] = {"2.4": Counter(), "5": Counter()}
    channel_seen: set[str] = set()
    ssids: dict[str, set[str]] = {}
    ssid_rows: Counter = Counter()
    found: dict[str, dict] = {}
    led2_times: list[float] = []
    distance, last_fix = 0.0, None

    for r in rows:
        kind, mac, label = TYPES[r["Type"]], r["MAC"].upper(), _name(r.get("SSID"))
        minute = int((r["_t"] - first_minute) // 60)
        per_minute[kind][minute] += 1
        if mac not in sightings[kind]:
            new_per_minute[kind][minute] += 1
        sightings[kind][mac] += 1

        level = _float(r.get("RSSI"))
        if level and level < 0:
            rssi[kind][min(max(int((level - RSSI_BINS[0]) // 10), 0), len(RSSI_BINS) - 1)] += 1

        if kind == "wifi":
            channel = int(_float(r.get("Channel")) or 0)
            if channel and mac not in channel_seen:
                channel_seen.add(mac)
                channels["2.4" if channel <= 14 else "5"][str(channel)] += 1
            if label:
                ssids.setdefault(label, set()).add(mac)
                ssid_rows[label] += 1

        lat, lon = _float(r.get("CurrentLatitude")), _float(r.get("CurrentLongitude"))
        if lat and lon:
            fix = (r["_t"], (lat, lon))
            if last_fix and fix[1] != last_fix[1]:
                step = _metres(last_fix[1], fix[1])
                if step <= MAX_SPEED_MPS * max(fix[0] - last_fix[0], 1):
                    distance += step
            last_fix = fix

        what = detect.kind_of(mac, label, r["Type"])
        if what:
            if what in LED2_KINDS:
                led2_times.append(r["_t"])
            d = found.setdefault(
                mac,
                {
                    "kind": what,
                    "label": detect.KINDS[what].label,
                    "confidence": detect.KINDS[what].confidence,
                    "mac": mac,
                    "name": label,
                    "type": kind,
                    "first_seen": r["_t"],
                    "sightings": 0,
                    "best_rssi": None,
                    "lat": lat,
                    "lon": lon,
                },
            )
            d["last_seen"] = r["_t"]
            d["sightings"] += 1
            if level and level < 0 and (d["best_rssi"] is None or level > d["best_rssi"]):
                d["best_rssi"], d["lat"], d["lon"] = level, lat, lon

    led2 = [0] * n_minutes
    spans = _lit_seconds(led2_times)
    for lo, hi in spans:
        for m in range(int((lo - first_minute) // 60), min(int((hi - first_minute) // 60) + 1, n_minutes)):
            m_lo = first_minute + m * 60
            led2[m] += int(max(0, min(hi, m_lo + 60) - max(lo, m_lo)))

    top = sorted(ssid_rows, key=lambda s: (-ssid_rows[s], s))[:TOP_NETWORKS]
    named = set().union(*ssids.values()) if ssids else set()
    return {
        "file": name,
        "start": start,
        "end": end,
        "duration_seconds": end - start,
        "gaps": [
            {"start": a["_t"], "end": b["_t"]} for a, b in zip(rows, rows[1:]) if b["_t"] - a["_t"] >= GAP_SECONDS
        ],
        "rows": len(rows),
        "partial_rows": partial,
        "nul_bytes": text.count("\0"),
        "devices": {t: len(c) for t, c in sightings.items()},
        "sightings": {t: sum(c.values()) for t, c in sightings.items()},
        "distance_km": round(distance / 1000, 2),
        "led2": {
            "lit_seconds": sum(led2),
            "spans": len(spans),
            "per_minute": led2,
        },
        "timeline": {"start": first_minute, "step": 60, "rows": per_minute, "new": new_per_minute},
        "rssi": {"bins": RSSI_BINS, "rows": rssi},
        "channels": {band: dict(sorted(c.items(), key=lambda kv: int(kv[0]))) for band, c in channels.items()},
        "repeat_sightings": {t: _sighting_buckets(c) for t, c in sightings.items()},
        "top_networks": [{"ssid": s, "sightings": ssid_rows[s], "bssids": len(ssids[s])} for s in top],
        "hidden_networks": len(set(sightings["wifi"]) - named),
        "detections": sorted(found.values(), key=lambda d: d["first_seen"])[:MAX_DETECTIONS],
    }


def build(path: Path) -> dict | None:
    """The report for a .wiglecsv file; None for anything else, or an empty one."""
    if path.suffix != ".wiglecsv":
        return None
    return build_text(path.name, path.read_text(errors="replace"))
