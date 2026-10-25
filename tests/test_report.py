"""report.py: the after-action report for one drive's .wiglecsv.
Every address, name and place here is made up."""

from __future__ import annotations

from unittest import mock

import report
from observability import RunStats
from pi_client import RemoteFile
from sync import REPORT_EVENT
from sync import sync_file
from tests.test_sync import _settings

HEADER = (
    "WigleWifi-1.4,appRelease=Kismet\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
)
T0 = 1792713600.0  # 2026-10-23 00:00:00 UTC


def row(mac, name, when, kind="WIFI", channel=6, rssi=-60, lat=40.0, lon=-75.0):
    stamp = f"2026-10-23 00:{when // 60:02d}:{when % 60:02d}"
    return f"{mac},{name},[ESS],{stamp},{channel},{rssi},{lat},{lon},0,0,{kind}\n"


def build(*rows, tail=""):
    return report.build_text("Kismet-test.wiglecsv", HEADER + "".join(rows) + tail)


def test_counts_devices_and_sightings_per_type():
    r = build(
        row("aa:00:00:00:00:01", "Neighbours", 0),
        row("aa:00:00:00:00:01", "Neighbours", 30),
        row("aa:00:00:00:00:02", "", 65),
        row("bb:00:00:00:00:01", "", 70, kind="BLE", rssi=0),
        row("cc:00:00:00:00:01", "Speaker", 80, kind="BT", rssi=0),
    )
    assert r["start"] == T0 and r["duration_seconds"] == 80
    assert r["devices"] == {"wifi": 2, "ble": 1, "bt": 1}
    assert r["sightings"] == {"wifi": 3, "ble": 1, "bt": 1}
    assert r["timeline"]["rows"]["wifi"] == [2, 1]
    assert r["timeline"]["new"]["wifi"] == [1, 1]  # the repeat at 30 s is not new
    assert r["hidden_networks"] == 1
    assert r["top_networks"] == [{"ssid": "Neighbours", "sightings": 2, "bssids": 1}]
    assert r["repeat_sightings"]["wifi"][:2] == [{"label": "1", "devices": 1}, {"label": "2", "devices": 1}]


def test_rssi_zero_is_no_reading():
    r = build(row("aa:00:00:00:00:01", "x", 0, rssi=-55), row("bb:00:00:00:00:01", "", 1, kind="BLE", rssi=0))
    assert sum(r["rssi"]["rows"]["wifi"]) == 1
    assert r["rssi"]["rows"]["wifi"][r["rssi"]["bins"].index(-60)] == 1
    assert sum(r["rssi"]["rows"]["ble"]) == 0


def test_each_network_counts_once_on_its_band():
    r = build(
        row("aa:00:00:00:00:01", "x", 0, channel=6),
        row("aa:00:00:00:00:01", "x", 5, channel=6),
        row("aa:00:00:00:00:02", "y", 9, channel=149),
    )
    assert r["channels"] == {"2.4": {"6": 1}, "5": {"149": 1}}


def test_led2_on_time_follows_the_60_second_window():
    # Two sightings 30 s apart keep LED2 lit 0-90 s; another at 300 s lights it again.
    flock = "12:34:56:78:9a:bc"
    r = build(
        row(flock, "Flock-A1B2C3", 0),
        row(flock, "Flock-A1B2C3", 30),
        row("aa:00:00:00:00:01", "x", 200),
        row(flock, "Flock-A1B2C3", 300, rssi=-48, lat=40.01, lon=-75.01),
        row("aa:00:00:00:00:01", "x", 420),
    )
    assert r["led2"]["lit_seconds"] == 150
    assert r["led2"]["spans"] == 2
    assert r["led2"]["per_minute"] == [60, 30, 0, 0, 0, 60, 0, 0]
    [d] = r["detections"]
    assert d["kind"] == "flock" and d["sightings"] == 3 and d["name"] == "Flock-A1B2C3"
    assert (d["first_seen"], d["last_seen"]) == (T0, T0 + 300)
    assert (d["best_rssi"], d["lat"], d["lon"]) == (-48, 40.01, -75.01)  # where it was loudest


def test_low_confidence_kinds_are_listed_but_leave_led2_off():
    r = build(row("aa:00:00:00:00:09", "HC-05", 0, kind="BT"))
    assert r["detections"][0]["kind"] == "skimmer"
    assert r["led2"]["lit_seconds"] == 0


def test_a_drive_cut_off_mid_row_still_reports():
    # Power pulled: a half-written last row, then NUL padding.
    r = build(row("aa:00:00:00:00:01", "x", 0), tail="aa:00:00:00:00:02,y,[ESS],2026-10-23 00:0" + "\0" * 64)
    assert r["rows"] == 1 and r["partial_rows"] == 1 and r["nul_bytes"] == 64


def test_distance_skips_gps_jumps():
    # ~1.1 km per 0.01 degree of latitude; the jump to 41.0 in 10 s is a bad fix.
    r = build(
        row("aa:00:00:00:00:01", "x", 0, lat=40.00),
        row("aa:00:00:00:00:02", "x", 60, lat=40.01),
        row("aa:00:00:00:00:03", "x", 70, lat=41.00),
        row("aa:00:00:00:00:04", "x", 130, lat=41.01),
    )
    assert 2.1 < r["distance_km"] < 2.3


def test_nothing_to_report(tmp_path):
    assert build() is None
    db = tmp_path / "Kismet-1.kismet"
    db.write_bytes(b"SQLite format 3\0")
    assert report.build(db) is None


# A drive: two sightings about 1 km apart (one that never moved is never uploaded, #26).
DRIVE = HEADER + row("aa:00:00:00:00:01", "x", 0) + row("aa:00:00:00:00:02", "y", 60, lat=40.01)


def _sync(tmp_path, text):
    csv = tmp_path / "Kismet-1.wiglecsv"
    csv.write_text(text)
    pi, wigle, stats = mock.Mock(), mock.Mock(), RunStats()
    pi.download.return_value = csv
    wigle.upload.return_value = {"results": []}
    remote = RemoteFile(csv.name, f"/r/{csv.name}", csv.stat().st_size, 0)
    with mock.patch("sync.log") as log:
        sync_file(pi, wigle, remote, _settings(), stats)
    return log, wigle, stats


def test_sync_logs_one_report_per_file(tmp_path):
    log, _, _ = _sync(tmp_path, DRIVE)
    [call] = [c for c in log.info.call_args_list if c.args == (REPORT_EVENT,)]
    assert call.kwargs["file"] == "Kismet-1.wiglecsv"
    assert call.kwargs["report"]["rows"] == 2


def test_a_failed_report_never_holds_up_the_upload(tmp_path):
    with mock.patch("report.build", side_effect=ValueError("bad row")):
        log, wigle, stats = _sync(tmp_path, DRIVE)
    wigle.upload.assert_called_once()
    assert stats.uploaded == 1
    log.warning.assert_called_once()


def test_silences_of_five_minutes_or_more_are_gaps():
    r = build(row("aa:00:00:00:00:01", "x", 0), row("aa:00:00:00:00:01", "x", 299), row("aa:00:00:00:00:01", "x", 900))
    assert r["gaps"] == [{"start": T0 + 299, "end": T0 + 900}]


def test_names_lose_kismets_octal_escapes():
    r = build(row("aa:00:00:00:00:01", r"Sam\342\200\231s Phone", 0))
    assert r["top_networks"][0]["ssid"] == "Sam’s Phone"
    assert report._name(r"caf\303") == "caf�"  # a cut-off character becomes one replacement mark
