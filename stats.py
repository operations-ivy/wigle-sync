from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests
import structlog

from sync import pi_is_online
from wigle_client import WIGLE_API_URL

log = structlog.get_logger()

PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "http://kube-prometheus-stack-prometheus.monitoring.svc.cluster.local:9090"
)
LOKI_URL = os.environ.get("LOKI_URL", "http://loki.monitoring.svc.cluster.local:3100")
NAMESPACE = os.environ.get("SYNC_NAMESPACE", "wigle")
CRONJOB = os.environ.get("SYNC_CRONJOB", "wigle-sync")
# Loki (and so run history / errors) keeps 4 days.
HISTORY_WINDOW_SECONDS = 4 * 24 * 3600

# WiGLE's API is rate limited per day; the numbers only move when an upload
# finishes processing, so there's no point asking more often than this.
WIGLE_TTL_SECONDS = 600
PI_PROBE_TTL_SECONDS = 30
STATUS_TTL_SECONDS = 15


class _Cache:
    """Tiny thread-safe TTL cache so several open consoles don't multiply upstream calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl: float, fn: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._entries.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._entries[key] = (time.time(), value)
        return value


_cache = _Cache()


def _section(name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    # One dead upstream (WiGLE down, Loki restarting) shouldn't blank the whole console.
    try:
        return fn()
    except Exception as e:
        log.warning("Status section failed", section=name, error=str(e))
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
        timeout=5,
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
    v = _prom_values('{__name__=~"wigle_sync_.+"}')
    if not v:
        return {"error": "no wigle_sync metrics in Prometheus yet"}
    return {k.removeprefix("wigle_sync_"): val for k, val in v.items()}


def pi() -> dict[str, Any]:
    host = os.environ.get("PI_HOST", "")
    port = int(os.environ.get("PI_PORT", "22"))
    online = _cache.get("pi", PI_PROBE_TTL_SECONDS, lambda: pi_is_online(host, port, timeout=1.5))
    return {"host": host, "online_now": online}


def history() -> dict[str, Any]:
    runs = _loki_lines(f'{{namespace="{NAMESPACE}"}} |= "Sync complete"', limit=120)
    keys = ("pi_online", "files_found", "uploaded", "archived_empty", "failed", "bytes_uploaded", "duration_seconds")
    return {"runs": [{"at": r["_ts"], **{k: r.get(k) for k in keys}} for r in runs]}


def errors() -> dict[str, Any]:
    lines = _loki_lines(f'{{namespace="{NAMESPACE}"}} | json | level=~"error|warning"', limit=30)
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


_UPLOAD_NAME = re.compile(r"^\d+_(.+?\.(?:kismet|wiglecsv))")


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
        uploads.append(
            {
                "transid": t["transid"],
                "file": match.group(1) if match else t.get("fileName"),
                "size": t.get("fileSize"),
                "status": t.get("status"),
                "percent_done": t.get("percentDone"),
                "new_gps": t.get("discoveredGps"),
                "total_gps": t.get("totalGps"),
                "queued_at": t.get("firstTime"),
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


def collect() -> dict[str, Any]:
    def build() -> dict[str, Any]:
        return {
            "generated_at": time.time(),
            "schedule": _section("schedule", schedule),
            "last_run": _section("last_run", last_run),
            "pi": _section("pi", pi),
            "history": _section("history", history),
            "errors": _section("errors", errors),
            "wigle": _section("wigle", wigle),
        }

    return _cache.get("status", STATUS_TTL_SECONDS, build)
