from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

import brick_observe
import requests
from prometheus_client import CollectorRegistry
from prometheus_client import Gauge
from prometheus_client import pushadd_to_gateway
from prometheus_client.parser import text_string_to_metric_families

import detect

SERVICE_NAME = "wigle-sync"
DETECTIONS = "wigle_sync_last_sync_detections"
# Running totals across every run. The Pushgateway keeps them on a persistent
# volume, so each run that uploads reads them back and pushes them increased.
UPLOADED_TOTAL = "wigle_sync_files_uploaded_since_launch"
BYTES_TOTAL = "wigle_sync_bytes_uploaded_since_launch"
TOTALS = {
    UPLOADED_TOTAL: "Files uploaded to WiGLE across every run",
    BYTES_TOTAL: "Bytes (post-compression) uploaded to WiGLE across every run",
}

log = brick_observe.get_logger(__name__)


@dataclass
class RunStats:
    pi_online: bool = False
    files_found: int = 0
    uploaded: int = 0
    archived_empty: int = 0
    # Sessions where the Pi never moved (home-only, #26): archived on the Pi, never uploaded.
    archived_stationary: int = 0
    # Already uploaded by the ledger (#21): archived on the Pi, not sent again.
    already_uploaded: int = 0
    # Not a format worth sending (see Settings.upload_extensions): archived, kept on the Pi.
    archived_kept: int = 0
    failed: int = 0
    # Left on the Pi because WiGLE was unreachable (e.g. the internet was down). Not a failure.
    deferred: int = 0
    bytes_uploaded: int = 0
    duration_seconds: float = 0.0
    # Surveillance and tracking devices on the drives synced this run, by kind (detect.py).
    detections: Counter = field(default_factory=Counter)

    @property
    def succeeded(self) -> bool:
        return self.failed == 0


def pushed_totals(gateway: str) -> dict[str, float]:
    """The lifetime totals the Pushgateway holds now, by metric name.

    A total that's missing is left out rather than read as 0: the gateway
    losing its data looks the same as never having had it, and restarting a
    total from this run's count would silently lose the history. push_metrics
    recounts a missing one from WiGLE instead (#20).
    """
    url = gateway if "://" in gateway else f"http://{gateway}"
    resp = requests.get(f"{url.rstrip('/')}/metrics", timeout=10)
    resp.raise_for_status()
    found = {}
    for family in text_string_to_metric_families(resp.text):
        for sample in family.samples:
            if sample.name in TOTALS and sample.labels.get("job") == SERVICE_NAME:
                found[sample.name] = sample.value
    return found


def _totals(gateway: str, stats: RunStats, recount: Callable[[], dict[str, float]] | None) -> dict[str, float]:
    """The lifetime totals to push: the gateway's plus this run's, or WiGLE's
    recount for one the gateway lost. Never a total restarted from this run."""
    try:
        current = pushed_totals(gateway)
    except Exception:
        # Pushing a total without the old one would reset it; try again next run.
        log.warning("Failed to read the upload totals, not updating them", uploaded=stats.uploaded, exc_info=True)
        return {}
    recounted: dict[str, float] = {}
    if recount and any(name not in current for name in TOTALS):
        try:
            recounted = recount()
        except Exception:
            log.warning("Failed to recount the upload totals from WiGLE", exc_info=True)
        else:
            log.info("Recounted the upload totals missing from the Pushgateway from WiGLE", **recounted)
    added = {UPLOADED_TOTAL: stats.uploaded, BYTES_TOTAL: stats.bytes_uploaded}
    out = {}
    for name in TOTALS:
        if name in current:
            if stats.uploaded:
                out[name] = current[name] + added[name]
        elif name in recounted:
            out[name] = recounted[name]
        elif stats.uploaded:
            log.warning("Upload total missing from the Pushgateway, not resetting it", metric=name, added=added[name])
    return out


def push_totals(gateway: str, totals: dict[str, float]) -> None:
    """Push lifetime totals as given, e.g. to seed them; other metrics are untouched."""
    registry = CollectorRegistry()
    for name, value in totals.items():
        Gauge(name, TOTALS[name], registry=registry).set(value)
    pushadd_to_gateway(gateway, job=SERVICE_NAME, registry=registry, timeout=10)


def push_metrics(stats: RunStats, finished_at: float, recount: Callable[[], dict[str, float]] | None = None) -> None:
    """Push this run's results to the Pushgateway, which Prometheus scrapes.

    A CronJob pod is gone long before Prometheus would scrape it, hence push.
    pushadd only replaces the metrics sent, so `last_success` and `last_pi_online`
    keep their previous values on runs where they don't apply — which is what
    makes "hours since the Pi last synced" answerable from Prometheus.

    recount gives the lifetime totals from WiGLE's upload history (which
    already includes this run's uploads). With it, every run checks the
    gateway's totals and puts back any that went missing, so a gateway that
    lost its data heals itself on the next run (#20).
    """
    gateway = os.environ.get("PUSHGATEWAY_URL")
    if not gateway:
        return

    registry = CollectorRegistry()

    def gauge(name: str, doc: str, value: float) -> None:
        Gauge(f"wigle_sync_{name}", doc, registry=registry).set(value)

    gauge("last_run_timestamp_seconds", "When the sync last ran", finished_at)
    gauge("pi_online", "Whether the Pi answered on the last run (1/0)", int(stats.pi_online))
    gauge("run_duration_seconds", "Duration of the last run", stats.duration_seconds)
    gauge("files_found", "Capture files ready on the Pi in the last run", stats.files_found)
    gauge("files_uploaded", "Files uploaded to WiGLE in the last run", stats.uploaded)
    gauge("files_archived_empty", "Empty files archived without upload in the last run", stats.archived_empty)
    gauge(
        "files_archived_stationary",
        "Sessions where the Pi never moved, archived without upload in the last run",
        stats.archived_stationary,
    )
    gauge(
        "files_already_uploaded",
        "Files the ledger says were already uploaded, archived in the last run",
        stats.already_uploaded,
    )
    gauge(
        "files_archived_kept",
        "Files archived on the Pi without upload (not an upload format) in the last run",
        stats.archived_kept,
    )
    gauge("files_failed", "Files that failed to sync in the last run", stats.failed)
    gauge("files_deferred", "Files left on the Pi in the last run because WiGLE was unreachable", stats.deferred)
    gauge("bytes_uploaded", "Bytes (post-compression) uploaded to WiGLE in the last run", stats.bytes_uploaded)
    if stats.pi_online:
        gauge("last_pi_online_timestamp_seconds", "When the Pi was last reachable", finished_at)
        # The last run that actually synced with the Pi; runs while it's away don't replace these.
        gauge("last_sync_files_uploaded", "Files uploaded by the last run that found the Pi", stats.uploaded)
        gauge("last_sync_bytes_uploaded", "Bytes uploaded by the last run that found the Pi", stats.bytes_uploaded)
        gauge("last_sync_files_failed", "Files that failed in the last run that found the Pi", stats.failed)
        gauge("last_sync_files_deferred", "Files deferred in the last run that found the Pi", stats.deferred)
    if stats.pi_online:
        # Every kind, 0 included, so a kind not seen this time reads 0, not its old count.
        detections = Gauge(
            DETECTIONS,
            "Surveillance and tracking devices on the drives the last run that found "
            "the Pi synced, by kind (detect.py)",
            ["kind", "confidence"],
            registry=registry,
        )
        for kind in detect.KINDS.values():
            detections.labels(kind.key, kind.confidence).set(stats.detections.get(kind.key, 0))
    if stats.pi_online and stats.succeeded and not stats.deferred:
        gauge("last_success_timestamp_seconds", "When a sync with the Pi last completed cleanly", finished_at)
    if stats.uploaded or recount:
        for name, value in _totals(gateway, stats, recount).items():
            gauge(name.removeprefix("wigle_sync_"), TOTALS[name], value)

    try:
        pushadd_to_gateway(gateway, job=SERVICE_NAME, registry=registry, timeout=10)
    except Exception:
        # Metrics are best-effort; never fail (or mask the result of) a sync over them.
        log.warning("Failed to push metrics", gateway=gateway, exc_info=True)
