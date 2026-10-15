"""wigle-console's Prometheus metrics, served at /metrics (console.py):
requests by route, the radar's refreshes and what it holds, status sections
that failed, and the WiGLE account's standing from the console's own cached
fetch (never a WiGLE call of its own at scrape time).

Pushed batch metrics (wigle_sync_*) are the sync job's; these are the
console's. Counts only, never a network's name or address.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import Counter
from prometheus_client import Gauge
from prometheus_client import Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY

REQUESTS = Counter("wigle_console_http_requests_total", "HTTP requests served.", ["route", "code"])
REQUEST_SECONDS = Histogram("wigle_console_http_request_duration_seconds", "Time to answer a request.", ["route"])
RADAR_REFRESHES = Counter("wigle_console_radar_refreshes_total", "Radar refreshes from WiGLE, by result.", ["result"])
RADAR_REFRESH_SECONDS = Histogram(
    "wigle_console_radar_refresh_duration_seconds",
    "Time a radar refresh took.",
    buckets=(1, 2, 5, 10, 30, 60, 120, 300, 600),
)
RADAR_LAST_SUCCESS = Gauge("wigle_console_radar_last_success_timestamp_seconds", "When the radar last refreshed.")
KML_FETCHES = Counter("wigle_console_radar_kml_fetches_total", "Drives' KML files fetched from WiGLE.")
SECTION_FAILURES = Counter(
    "wigle_console_status_section_failures_total", "Status page sections that failed (an upstream down).", ["section"]
)
for result in ("ok", "error"):
    RADAR_REFRESHES.labels(result)

# WiGLE's /stats/user fields, as gauges.
ACCOUNT = {
    "rank": ("wigle_account_rank", "The account's all-time rank on WiGLE (lower is better)."),
    "month_rank": ("wigle_account_month_rank", "The account's rank this month."),
    "wifi_gps": ("wigle_account_wifi_networks_with_gps", "Wi-Fi networks first found by the account, with GPS."),
    "wifi": ("wigle_account_wifi_networks", "Wi-Fi networks first found by the account."),
    "bt": ("wigle_account_bluetooth_devices", "Bluetooth devices first found by the account."),
    "locations": ("wigle_account_wifi_locations", "Wi-Fi location observations by the account."),
    "month_events": ("wigle_account_month_events", "Observations this month."),
}


class _Live:
    """Read at scrape time: the radar's contents and the cached WiGLE stats."""

    def __init__(self, radar, wigle_cached):
        self.radar, self.wigle_cached = radar, wigle_cached

    def collect(self):
        drives = GaugeMetricFamily("wigle_console_radar_drives", "Drives the radar holds, by layer.", labels=["layer"])
        counts = self.radar.counts()
        drives.add_metric(["recent"], counts["recent"])
        drives.add_metric(["ghost"], counts["ghost"])
        yield drives
        yield GaugeMetricFamily(
            "wigle_console_radar_contacts", "Networks plotted from the recent drives.", value=counts["contacts"]
        )
        stats: dict[str, Any] | None = self.wigle_cached()
        if not stats:
            return
        for key, (name, doc) in ACCOUNT.items():
            if isinstance(stats.get(key), (int, float)):
                yield GaugeMetricFamily(name, doc, value=stats[key])
        pending = sum(1 for u in stats.get("uploads", []) if u.get("status") not in ("D", "E"))
        yield GaugeMetricFamily(
            "wigle_account_uploads_processing", "Uploads WiGLE hasn't finished processing.", value=pending
        )


def register_live(radar, wigle_cached) -> None:
    REGISTRY.register(_Live(radar, wigle_cached))
