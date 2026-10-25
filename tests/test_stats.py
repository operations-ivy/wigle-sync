from __future__ import annotations

import threading
from unittest import mock

import stats


def test_one_dead_source_does_not_blank_the_console():
    with (
        mock.patch.object(stats, "_cache", stats._Cache()),
        mock.patch.object(stats, "schedule", return_value={"next_run": 1.0}),
        mock.patch.object(stats, "last_run", side_effect=ConnectionError("prometheus down")),
        mock.patch.object(stats, "pi", return_value={"online_now": False}),
        mock.patch.object(stats, "history", return_value={"runs": []}),
        mock.patch.object(stats, "errors", return_value={"items": []}),
        mock.patch.object(stats, "wigle", return_value={"uploads": []}),
    ):
        status = stats.collect()

    assert status["schedule"] == {"next_run": 1.0}
    assert status["last_run"]["error"].startswith("ConnectionError")


def test_wigle_timestamps_become_epoch_seconds():
    assert stats._iso_to_epoch("2026-10-07T22:56:42.000Z") == 1791413802.0
    assert stats._iso_to_epoch(None) is None
    assert stats._iso_to_epoch("not a date") is None


def test_capture_time_comes_from_the_kismet_filename_in_utc():
    assert stats._captured_at("Kismet-20261009-01-04-41-1.kismet") == 1791507881.0
    assert stats._captured_at("something-else.csv") is None
    assert stats._captured_at(None) is None


def test_upload_names_are_recovered_from_wigles_mangled_filenames():
    mangled = "1790276593_Kismet-20260924-15-14-14-1.wiglecsv0Kismet-20260924-15-14-14-1.wiglecsv"
    assert stats._UPLOAD_NAME.match(mangled).group(1) == "Kismet-20260924-15-14-14-1.wiglecsv"


def test_cache_reuses_values_within_ttl():
    cache = stats._Cache()
    fn = mock.Mock(side_effect=[1, 2])
    assert cache.get("k", 60, fn) == 1
    assert cache.get("k", 60, fn) == 1
    fn.assert_called_once()


def _join_refreshes():
    # Only the cache's own refresh threads: other tests (the console's) leave
    # long-lived daemon threads running.
    for t in threading.enumerate():
        if t.name.startswith("cache-refresh-"):
            t.join(timeout=5)


def test_stale_value_is_served_while_refreshing_in_background():
    cache = stats._Cache()
    release = threading.Event()

    def slow():
        release.wait(5)
        return 2

    assert cache.get("k", 0, lambda: 1) == 1
    assert cache.get("k", 0, slow) == 1  # stale, returned without waiting
    assert cache.get("k", 0, slow) == 1  # one refresh at a time
    release.set()
    _join_refreshes()
    assert cache.get("k", 60, slow) == 2


def test_failed_refresh_keeps_the_stale_value():
    cache = stats._Cache()
    assert cache.get("k", 0, lambda: 1) == 1
    assert cache.get("k", 0, mock.Mock(side_effect=OSError("loki down"))) == 1
    _join_refreshes()
    assert cache.get("k", 60, lambda: 3) == 1


def test_peek_never_fetches():
    cache = stats._Cache()
    assert cache.peek("k") is None
    cache.get("k", 60, lambda: {"rank": 3})
    assert cache.peek("k") == {"rank": 3}


def test_detections_read_by_kind_and_unknown_kinds_ignored():
    result = [
        {"metric": {"__name__": "wigle_sync_last_sync_detections", "kind": k, "confidence": c}, "value": [0, v]}
        for k, c, v in (("flock", "medium", "2"), ("skimmer", "low", "3"), ("axon", "high", "0"), ("old", "high", "9"))
    ]
    with mock.patch.object(stats, "_prom", return_value=result) as prom:
        found = stats.detections()
    assert prom.call_args.args[0] == "wigle_sync_last_sync_detections"
    assert found["kinds"]["flock"] == {"label": "Flock ALPR camera", "confidence": "medium", "count": 2}
    assert "old" not in found["kinds"]
    assert found["alerting"] == 2  # the low-confidence skimmer guesses don't count


def test_last_run_leaves_the_labelled_detections_out():
    with mock.patch.object(stats, "_prom_values", return_value={"wigle_sync_pi_online": 1.0}) as values:
        assert stats.last_run() == {"pi_online": 1.0}
    assert '__name__!="wigle_sync_last_sync_detections"' in values.call_args.args[0]


def test_after_action_keeps_each_files_newest_report_newest_drive_first():
    def line(ts, name, start, rows):
        return {"_ts": ts, "event": stats.REPORT_EVENT, "report": {"file": name, "start": start, "rows": rows}}

    lines = [  # as Loki returns them: newest line first
        line(300, "Kismet-a.wiglecsv", 10, rows=2),  # re-uploaded: this one wins
        line(200, "Kismet-b.wiglecsv", 20, rows=5),
        line(100, "Kismet-a.wiglecsv", 10, rows=1),
        {"_ts": 50, "event": "Sync complete"},
    ]
    with mock.patch.object(stats, "_loki_lines", return_value=lines):
        reports = stats.after_action()["reports"]
    assert [(r["file"], r["rows"], r["logged_at"]) for r in reports] == [
        ("Kismet-b.wiglecsv", 5, 200),
        ("Kismet-a.wiglecsv", 2, 300),
    ]
