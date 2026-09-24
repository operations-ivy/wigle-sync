from __future__ import annotations

from unittest import mock

from observability import push_metrics
from observability import RunStats


def _pushed(stats: RunStats) -> dict[str, float]:
    with mock.patch.dict("os.environ", {"PUSHGATEWAY_URL": "http://gw:9091"}):
        with mock.patch("observability.pushadd_to_gateway") as push:
            push_metrics(stats, finished_at=1000.0)
    registry = push.call_args.kwargs["registry"]
    return {s.name: s.value for m in registry.collect() for s in m.samples}


def test_success_timestamp_only_pushed_for_clean_online_runs():
    assert _pushed(RunStats(pi_online=True, uploaded=2))["wigle_sync_last_success_timestamp_seconds"] == 1000.0

    offline = _pushed(RunStats(pi_online=False))
    assert "wigle_sync_last_success_timestamp_seconds" not in offline
    assert "wigle_sync_last_pi_online_timestamp_seconds" not in offline
    assert offline["wigle_sync_pi_online"] == 0

    failed = _pushed(RunStats(pi_online=True, failed=1))
    assert "wigle_sync_last_success_timestamp_seconds" not in failed
    assert failed["wigle_sync_last_pi_online_timestamp_seconds"] == 1000.0


def test_push_failure_does_not_raise():
    with mock.patch.dict("os.environ", {"PUSHGATEWAY_URL": "http://gw:9091"}):
        with mock.patch("observability.pushadd_to_gateway", side_effect=OSError("down")):
            push_metrics(RunStats(), finished_at=1000.0)
