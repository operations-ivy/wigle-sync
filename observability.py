from __future__ import annotations

import logging
import os
from dataclasses import dataclass

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

SERVICE_NAME = "wigle-sync"

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


def init_tracing() -> TracerProvider | None:
    """Ship spans to the in-cluster OTel collector (-> Tempo). No-op when the endpoint isn't set, e.g. locally."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
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
    bytes_uploaded: int = 0
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.failed == 0


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
    gauge("bytes_uploaded", "Bytes (post-compression) uploaded to WiGLE in the last run", stats.bytes_uploaded)
    if stats.pi_online:
        gauge("last_pi_online_timestamp_seconds", "When the Pi was last reachable", finished_at)
    if stats.pi_online and stats.succeeded:
        gauge("last_success_timestamp_seconds", "When a sync with the Pi last completed cleanly", finished_at)

    try:
        pushadd_to_gateway(gateway, job=SERVICE_NAME, registry=registry, timeout=10)
    except Exception:
        # Metrics are best-effort; never fail (or mask the result of) a sync over them.
        log.warning("Failed to push metrics", gateway=gateway, exc_info=True)
