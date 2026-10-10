from __future__ import annotations

from unittest import mock

from observability import BYTES_TOTAL
from observability import push_metrics
from observability import push_totals
from observability import RunStats
from observability import UPLOADED_TOTAL


GATEWAY_WITH_TOTAL = """# TYPE wigle_sync_files_uploaded_since_launch gauge
wigle_sync_files_uploaded_since_launch{instance="",job="wigle-sync"} 40
wigle_sync_files_uploaded_since_launch{instance="",job="other"} 999
# TYPE wigle_sync_bytes_uploaded_since_launch gauge
wigle_sync_bytes_uploaded_since_launch{instance="",job="wigle-sync"} 5000
"""


def _pushed(stats: RunStats, gateway_metrics: str = "", gateway_error: Exception | None = None) -> dict[str, float]:
    resp = mock.Mock(text=gateway_metrics)
    with mock.patch.dict("os.environ", {"PUSHGATEWAY_URL": "http://gw:9091"}):
        with (
            mock.patch("observability.pushadd_to_gateway") as push,
            mock.patch("observability.requests.get", return_value=resp, side_effect=gateway_error) as get,
        ):
            push_metrics(stats, finished_at=1000.0)
    if stats.uploaded:
        assert get.call_args.args[0] == "http://gw:9091/metrics"
    else:
        get.assert_not_called()
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


def test_upload_totals_add_this_run_to_the_pushed_totals():
    pushed = _pushed(RunStats(pi_online=True, uploaded=3, bytes_uploaded=700), GATEWAY_WITH_TOTAL)
    assert pushed[UPLOADED_TOTAL] == 43
    assert pushed[BYTES_TOTAL] == 5700
    assert pushed["wigle_sync_last_sync_bytes_uploaded"] == 700


def test_upload_totals_not_restarted_when_missing_from_the_gateway():
    # An empty gateway may have lost its data; restarting from this run would hide that.
    pushed = _pushed(RunStats(pi_online=True, uploaded=2, bytes_uploaded=100), "")
    assert UPLOADED_TOTAL not in pushed
    assert BYTES_TOTAL not in pushed
    assert pushed["wigle_sync_files_uploaded"] == 2


def test_only_the_missing_total_is_skipped():
    files_only = GATEWAY_WITH_TOTAL.split("# TYPE wigle_sync_bytes")[0]
    pushed = _pushed(RunStats(pi_online=True, uploaded=1, bytes_uploaded=10), files_only)
    assert pushed[UPLOADED_TOTAL] == 41
    assert BYTES_TOTAL not in pushed


def test_push_totals_sends_only_the_totals():
    with mock.patch("observability.pushadd_to_gateway") as push:
        push_totals("http://gw:9091", {UPLOADED_TOTAL: 105, BYTES_TOTAL: 123456})
    registry = push.call_args.kwargs["registry"]
    assert {s.name: s.value for m in registry.collect() for s in m.samples} == {
        UPLOADED_TOTAL: 105,
        BYTES_TOTAL: 123456,
    }
    assert push.call_args.kwargs["job"] == "wigle-sync"


def test_upload_total_untouched_when_nothing_uploaded():
    assert UPLOADED_TOTAL not in _pushed(RunStats(pi_online=True), GATEWAY_WITH_TOTAL)


def test_upload_total_not_reset_when_gateway_unreadable():
    pushed = _pushed(RunStats(pi_online=True, uploaded=2), gateway_error=OSError("down"))
    assert UPLOADED_TOTAL not in pushed
    assert pushed["wigle_sync_files_uploaded"] == 2


def test_deferred_run_is_not_a_failure_or_a_success():
    pushed = _pushed(RunStats(pi_online=True, deferred=4))
    assert pushed["wigle_sync_files_failed"] == 0
    assert pushed["wigle_sync_files_deferred"] == 4
    assert pushed["wigle_sync_last_sync_files_deferred"] == 4
    assert "wigle_sync_last_success_timestamp_seconds" not in pushed


def test_last_sync_gauges_only_pushed_when_the_pi_was_home():
    home = _pushed(RunStats(pi_online=True, uploaded=3, failed=1))
    assert home["wigle_sync_last_sync_files_uploaded"] == 3
    assert home["wigle_sync_last_sync_files_failed"] == 1

    away = _pushed(RunStats(pi_online=False))
    assert not any(k.startswith("wigle_sync_last_sync_") for k in away)
