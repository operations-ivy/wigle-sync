from __future__ import annotations

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


def test_upload_names_are_recovered_from_wigles_mangled_filenames():
    mangled = "1790276593_Kismet-20260924-15-14-14-1.wiglecsv0Kismet-20260924-15-14-14-1.wiglecsv"
    assert stats._UPLOAD_NAME.match(mangled).group(1) == "Kismet-20260924-15-14-14-1.wiglecsv"


def test_cache_reuses_values_within_ttl():
    cache = stats._Cache()
    fn = mock.Mock(side_effect=[1, 2])
    assert cache.get("k", 60, fn) == 1
    assert cache.get("k", 60, fn) == 1
    assert cache.get("k", 0, fn) == 2
