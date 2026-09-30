from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests
import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CollectorRegistry
from prometheus_client import Gauge
from prometheus_client import pushadd_to_gateway
from prometheus_client.parser import text_string_to_metric_families

SERVICE_NAME = "wigle-sync"
# Running total across every run. The Pushgateway keeps it on a persistent
# volume, so each run that uploads reads it back and pushes it increased.
UPLOADED_TOTAL = "wigle_sync_files_uploaded_since_launch"

log = structlog.get_logger()


def _add_trace_context(_: object, __: str, event_dict: dict) -> dict:
    # Lets Grafana jump from a Loki log line straight to its trace in Tempo.
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def init_logging() -> None:
    """JSON lines in k8s (promtail ships stdout to Loki); readable console output locally."""
    renderer = (
        structlog.processors.JSONRenderer()
        if os.environ.get("LOG_FORMAT", "console") == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_trace_context,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def init_tracing(service_name: str = SERVICE_NAME) -> TracerProvider | None:
    """Ship spans to the in-cluster OTel collector (-> Tempo). No-op when the endpoint isn't set, e.g. locally."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    RequestsInstrumentor().instrument()
    return provider


@dataclass
class RunStats:
    pi_online: bool = False
    files_found: int = 0
    uploaded: int = 0
    archived_empty: int = 0
    failed: int = 0
    # Left on the Pi because WiGLE was unreachable (e.g. the internet was down). Not a failure.
    deferred: int = 0
    bytes_uploaded: int = 0
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.failed == 0


def pushed_uploaded_total(gateway: str) -> float:
    """The lifetime upload total the Pushgateway holds now; 0 before it was ever pushed."""
    url = gateway if "://" in gateway else f"http://{gateway}"
    resp = requests.get(f"{url.rstrip('/')}/metrics", timeout=10)
    resp.raise_for_status()
    for family in text_string_to_metric_families(resp.text):
        for sample in family.samples:
            if sample.name == UPLOADED_TOTAL and sample.labels.get("job") == SERVICE_NAME:
                return sample.value
    return 0.0


def push_metrics(stats: RunStats, finished_at: float) -> None:
    """Push this run's results to the Pushgateway, which Prometheus scrapes.

    A CronJob pod is gone long before Prometheus would scrape it, hence push.
    pushadd only replaces the metrics sent, so `last_success` and `last_pi_online`
    keep their previous values on runs where they don't apply — which is what
    makes "hours since the Pi last synced" answerable from Prometheus.
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
    gauge("files_failed", "Files that failed to sync in the last run", stats.failed)
    gauge("files_deferred", "Files left on the Pi in the last run because WiGLE was unreachable", stats.deferred)
    gauge("bytes_uploaded", "Bytes (post-compression) uploaded to WiGLE in the last run", stats.bytes_uploaded)
    if stats.pi_online:
        gauge("last_pi_online_timestamp_seconds", "When the Pi was last reachable", finished_at)
        # The last run that actually synced with the Pi; runs while it's away don't replace these.
        gauge("last_sync_files_uploaded", "Files uploaded by the last run that found the Pi", stats.uploaded)
        gauge("last_sync_files_failed", "Files that failed in the last run that found the Pi", stats.failed)
        gauge("last_sync_files_deferred", "Files deferred in the last run that found the Pi", stats.deferred)
    if stats.pi_online and stats.succeeded and not stats.deferred:
        gauge("last_success_timestamp_seconds", "When a sync with the Pi last completed cleanly", finished_at)
    if stats.uploaded:
        try:
            total = pushed_uploaded_total(gateway) + stats.uploaded
        except Exception:
            # Pushing a total without the old one would reset it; leave it for a human instead.
            log.warning("Failed to read the upload total, not updating it", uploaded=stats.uploaded, exc_info=True)
        else:
            gauge("files_uploaded_since_launch", "Files uploaded to WiGLE across every run", total)

    try:
        pushadd_to_gateway(gateway, job=SERVICE_NAME, registry=registry, timeout=10)
    except Exception:
        # Metrics are best-effort; never fail (or mask the result of) a sync over them.
        log.warning("Failed to push metrics", gateway=gateway, exc_info=True)
