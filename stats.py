from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from itertools import combinations
from typing import Any

import requests
import brick_observe

import console_metrics
import detect
from config import split_list
from observability import DETECTIONS
from sync import pi_is_online
from sync import REPORT_EVENT
from wigle_client import WIGLE_API_URL

log = brick_observe.get_logger(__name__)

PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
)
LOKI_URL = os.environ.get("LOKI_URL", "http://loki.monitoring.svc.cluster.local:3100")
NAMESPACE = os.environ.get("SYNC_NAMESPACE", "wigle")
CRONJOB = os.environ.get("SYNC_CRONJOB", "wigle-sync")
# The sync job's container. Faults come only from it: the console's own hiccups
# (a slow Loki, say) are not worth showing on the console.
SYNC_CONTAINER = os.environ.get("SYNC_CONTAINER", "wigle-sync")
# Loki (and so run history / errors) keeps 4 days.
HISTORY_WINDOW_SECONDS = 4 * 24 * 3600
# After-action reports the console can switch between (one per drive file).
AFTER_ACTION_REPORTS = 12
# Two Pis' drives are the same drive, side by side, when they overlap for at
# least this share of the shorter one.
SAME_DRIVE_OVERLAP = 0.5
# The wardriving Pis, as the sync's PI_HOSTS (or PI_HOST alone, as Settings.from_env reads them).
PI_HOSTS = split_list(os.environ.get("PI_HOSTS", "")) or split_list(os.environ.get("PI_HOST", ""))

# WiGLE's API is rate limited per day; the numbers only move when an upload
# finishes processing, so there's no point asking more often than this.
WIGLE_TTL_SECONDS = 600
PI_PROBE_TTL_SECONDS = 30
STATUS_TTL_SECONDS = 15


class _Cache:
    """Thread-safe TTL cache with stale-while-revalidate.

    Past its TTL an entry is still returned at once, while one background thread
    per key refreshes it, so a viewer never waits on a slow upstream (a cold
    status build is ~10s, mostly Loki's 96h queries). Only a key's very first
    request, with nothing cached yet, waits for the value.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Any]] = {}
        self._refreshing: set[str] = set()

    def get(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._entries.get(key)
            if hit:
                if time.time() - hit[0] >= ttl and key not in self._refreshing:
                    self._refreshing.add(key)
                    threading.Thread(
                        target=self._refresh, args=(key, fn), name=f"cache-refresh-{key}", daemon=True
                    ).start()
                return hit[1]
        value = fn()
        with self._lock:
            self._entries[key] = (time.time(), value)
        return value

    def peek(self, key: str) -> Any:
        """The cached value, however old, or None; never fetches."""
        with self._lock:
            hit = self._entries.get(key)
        return hit[1] if hit else None

    def _refresh(self, key: str, fn: Callable[[], Any]) -> None:
        try:
            value = fn()
        except Exception as e:
            # Keep serving the stale value; the next request past the TTL tries again.
            log.warning("Cache refresh failed", key=key, error=str(e))
        else:
            with self._lock:
                self._entries[key] = (time.time(), value)
        finally:
            with self._lock:
                self._refreshing.discard(key)


_cache = _Cache()


def _section(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    # One dead upstream (WiGLE down, Loki restarting) shouldn't blank the whole console.
    try:
        return fn()
    except Exception as e:
        log.warning("Status section failed", section=name, error=str(e))
        console_metrics.SECTION_FAILURES.labels(name).inc()
        return {"error": f"{type(e).__name__}: {e}"}


def _prom(query: str) -> list[dict[str, Any]]:
    resp = requests.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=5)
    resp.raise_for_status()
    return resp.json()["data"]["result"]


def _prom_values(query: str) -> dict[str, float]:
    return {r["metric"]["__name__"]: float(r["value"][1]) for r in _prom(query)}


def _loki_lines(query: str, limit: int) -> list[dict[str, Any]]:
    now = time.time()
    resp = requests.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": query,
            "limit": limit,
            "start": int((now - HISTORY_WINDOW_SECONDS) * 1e9),
            "end": int(now * 1e9),
            "direction": "backward",
        },
        # Refreshes run in the background (see _Cache), so a slow 96h query costs
        # no one a wait; 5s used to cut it off and blank the panel.
        timeout=20,
    )
    resp.raise_for_status()
    lines = []
    for stream in resp.json()["data"]["result"]:
        for ts, line in stream["values"]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            entry["_ts"] = int(ts) / 1e9
            lines.append(entry)
    return sorted(lines, key=lambda e: e["_ts"], reverse=True)[:limit]


def schedule() -> dict[str, Any]:
    v = _prom_values(
        f'{{__name__=~"kube_cronjob_(next_schedule_time|status_last_schedule_time|spec_suspend|status_active)",'
        f'namespace="{NAMESPACE}",cronjob="{CRONJOB}"}}'
    )
    failed = _prom(f'sum(increase(kube_job_status_failed{{namespace="{NAMESPACE}"}}[24h]))')
    return {
        "next_run": v.get("kube_cronjob_next_schedule_time"),
        "last_scheduled": v.get("kube_cronjob_status_last_schedule_time"),
        "suspended": bool(v.get("kube_cronjob_spec_suspend")),
        "running": bool(v.get("kube_cronjob_status_active")),
        "failed_jobs_24h": round(float(failed[0]["value"][1])) if failed else 0,
    }


def last_run() -> dict[str, Any]:
    # Labelled series would collapse into one value by name: detections() and
    # pi() read those (wigle_sync_device_* is per Pi).
    v = _prom_values(f'{{__name__=~"wigle_sync_.+", __name__!="{DETECTIONS}", __name__!~"wigle_sync_device_.+"}}')
    if not v:
        return {"error": "no wigle_sync metrics in Prometheus yet"}
    return {k.removeprefix("wigle_sync_"): val for k, val in v.items()}


def detections() -> dict[str, Any]:
    """Surveillance and tracking devices on the drives the last sync with the
    Pi brought in, by kind (detect.py): counts only."""
    found = {}
    for r in _prom(DETECTIONS):
        kind = r["metric"].get("kind")
        if kind in detect.KINDS:
            k = detect.KINDS[kind]
            found[kind] = {"label": k.label, "confidence": k.confidence, "count": int(float(r["value"][1]))}
    alerting = sum(v["count"] for v in found.values() if v["confidence"] != "low")
    return {"kinds": found, "alerting": alerting}


def pi() -> dict[str, Any]:
    """Each Pi: home now (a probe), and when the sync last found it. `host`
    and `online_now` are the first Pi's, as before there was more than one."""
    port = int(os.environ.get("PI_PORT", "22"))
    try:
        seen = {
            r["metric"].get("pi"): float(r["value"][1])
            for r in _prom("wigle_sync_device_last_online_timestamp_seconds")
        }
    except (requests.RequestException, KeyError, ValueError) as e:
        log.warning("Couldn't read when the Pis were last found", error=str(e))
        seen = {}  # the probes still answer without Prometheus
    hosts = [
        {
            "host": host,
            "online_now": _cache.get(f"pi:{host}", PI_PROBE_TTL_SECONDS, lambda h=host: pi_is_online(h, port, 1.5)),
            "last_online": seen.get(host),
        }
        for host in PI_HOSTS
    ]
    first = hosts[0] if hosts else {"host": "", "online_now": False}
    return {"host": first["host"], "online_now": first["online_now"], "hosts": hosts}


def history() -> dict[str, Any]:
    runs = _loki_lines(f'{{namespace="{NAMESPACE}"}} |= "Sync complete"', limit=120)
    keys = (
        "pi_online",
        "files_found",
        "uploaded",
        "archived_empty",
        "archived_stationary",
        "already_uploaded",
        "archived_kept",
        "failed",
        "deferred",
        "bytes_uploaded",
        "duration_seconds",
    )
    return {"runs": [{"at": r["_ts"], **{k: r.get(k) for k in keys}} for r in runs]}


def errors() -> dict[str, Any]:
    lines = _loki_lines(
        f'{{namespace="{NAMESPACE}",container="{SYNC_CONTAINER}"}} | json | level=~"error|warning"', limit=30
    )
    out = []
    for e in lines:
        exc = (e.get("exception") or "").strip().splitlines()
        out.append(
            {
                "at": e["_ts"],
                "level": e.get("level"),
                "event": e.get("event"),
                "file": e.get("file"),
                "detail": exc[-1] if exc else e.get("error"),
                "trace_id": e.get("trace_id"),
            }
        )
    return {"items": out}


def after_action() -> dict[str, Any]:
    """The newest drives' after-action reports (report.py), newest drive first.
    One per file; Loki keeps 4 days, so a Pi left home longer has none."""
    lines = _loki_lines(f'{{namespace="{NAMESPACE}",container="{SYNC_CONTAINER}"}} |= "{REPORT_EVENT}"', limit=40)
    by_file: dict[tuple[str | None, str], dict[str, Any]] = {}
    for e in lines:  # newest line first: a re-upload's report replaces the old one
        r = e.get("report")
        if e.get("event") != REPORT_EVENT or not isinstance(r, dict) or "file" not in r:
            continue
        # Two Pis can name a session alike (the same start second): keyed by both.
        key = (e.get("pi"), r["file"])
        if key not in by_file:
            by_file[key] = {**r, "pi": e.get("pi"), "logged_at": e["_ts"]}
    reports = sorted(by_file.values(), key=lambda r: r.get("start") or 0, reverse=True)[:AFTER_ACTION_REPORTS]
    return {"reports": reports, "side_by_side": side_by_side(reports)}


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> float:
    """How much of the shorter drive the two share, 0 to 1; 0 if either
    has no start or end."""
    try:
        shared = min(a["end"], b["end"]) - max(a["start"], b["start"])
        shorter = min(a["end"] - a["start"], b["end"] - b["start"])
    except (KeyError, TypeError):
        return 0.0
    return max(0.0, shared) / shorter if shorter > 0 else 0.0


def _drive_numbers(r: dict[str, Any]) -> dict[str, Any]:
    devices = r.get("devices") or {}
    return {
        "pi": r.get("pi"),
        "file": r.get("file"),
        "start": r.get("start"),
        "duration_seconds": r.get("duration_seconds"),
        "distance_km": r.get("distance_km"),
        "rows": r.get("rows"),
        "wifi": devices.get("wifi"),
        "bluetooth": (devices.get("bt") or 0) + (devices.get("ble") or 0),
    }


def side_by_side(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drives two Pis made together (overlapping by SAME_DRIVE_OVERLAP or
    more), newest first, each with both Pis' numbers: what the parallel runs
    compare."""
    pairs = []
    for a, b in combinations(reports, 2):
        if not (a.get("pi") and b.get("pi")) or a["pi"] == b["pi"]:
            continue
        if (overlap := _overlap(a, b)) >= SAME_DRIVE_OVERLAP:
            pairs.append(
                {
                    "start": min(a["start"], b["start"]),
                    "overlap": round(overlap, 2),
                    "pis": [_drive_numbers(r) for r in sorted((a, b), key=_pi_rank)],
                }
            )
    return sorted(pairs, key=lambda p: p["start"], reverse=True)


def _pi_rank(report: dict[str, Any]) -> int:
    """A report's Pi's place in PI_HOSTS, unknown Pis last."""
    return PI_HOSTS.index(report["pi"]) if report["pi"] in PI_HOSTS else len(PI_HOSTS)


_UPLOAD_NAME = re.compile(r"^\d+_(.+?\.(?:kismet|wiglecsv))")
# Kismet names each session for its start time, in UTC.
_KISMET_START = re.compile(r"^Kismet-(\d{8}-\d{2}-\d{2}-\d{2})")


def _iso_to_epoch(value: str | None) -> float | None:
    """WiGLE's timestamps ("2026-10-07T22:56:42.000Z") as epoch seconds."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None
    except ValueError:
        return None


def _captured_at(name: str | None) -> float | None:
    """When the Kismet session in this file started, from its name."""
    match = _KISMET_START.match(name or "")
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d-%H-%M-%S").replace(tzinfo=timezone.utc).timestamp()


def _wigle() -> dict[str, Any]:
    auth = (os.environ["WIGLE_API_NAME"], os.environ["WIGLE_API_TOKEN"])
    headers = {"Accept": "application/json"}
    stats = requests.get(f"{WIGLE_API_URL}/stats/user", auth=auth, headers=headers, timeout=10)
    stats.raise_for_status()
    trans = requests.get(
        f"{WIGLE_API_URL}/file/transactions",
        params={"pagestart": 0, "pageend": 50},
        auth=auth,
        headers=headers,
        timeout=10,
    )
    trans.raise_for_status()

    s = stats.json()["statistics"]
    uploads = []
    for t in trans.json().get("results", []):
        match = _UPLOAD_NAME.match(t.get("fileName") or "")
        name = match.group(1) if match else t.get("fileName")
        uploads.append(
            {
                "transid": t["transid"],
                "file": name,
                "size": t.get("fileSize"),
                "status": t.get("status"),
                "percent_done": t.get("percentDone"),
                "new_gps": t.get("discoveredGps"),
                "total_gps": t.get("totalGps"),
                "queued_at": _iso_to_epoch(t.get("firstTime")),
                "captured_at": _captured_at(name),
            }
        )
    return {
        "user": s.get("userName"),
        "rank": s.get("rank"),
        "prev_rank": s.get("prevRank"),
        "month_rank": s.get("monthRank"),
        "prev_month_rank": s.get("prevMonthRank"),
        "wifi_gps": s.get("discoveredWiFiGPS"),
        "wifi": s.get("discoveredWiFi"),
        "bt": s.get("discoveredBt"),
        "locations": s.get("totalWiFiLocations"),
        "month_events": s.get("eventMonthCount"),
        "uploads": uploads,
        "fetched_at": time.time(),
    }


def wigle() -> dict[str, Any]:
    return _cache.get("wigle", WIGLE_TTL_SECONDS, _wigle)


def wigle_cached() -> dict[str, Any] | None:
    """WiGLE's stats as last fetched, for /metrics; None before the first fetch."""
    return _cache.peek("wigle")


def collect() -> dict[str, Any]:
    def build() -> dict[str, Any]:
        return {
            "generated_at": time.time(),
            "schedule": _section("schedule", schedule),
            "last_run": _section("last_run", last_run),
            "pi": _section("pi", pi),
            "history": _section("history", history),
            "errors": _section("errors", errors),
            "detections": _section("detections", detections),
            "after_action": _section("after_action", after_action),
            "wigle": _section("wigle", wigle),
        }

    return _cache.get("status", STATUS_TTL_SECONDS, build)
