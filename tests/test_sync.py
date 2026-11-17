from __future__ import annotations

import shutil
import socket
import sqlite3
from unittest import mock

from config import Settings
from observability import RunStats
from pi_client import RemoteFile
from sync import has_observations
from sync import pi_is_online
from sync import recover_kismet_db
from sync import run
from sync import span_attributes
from sync import stationary
from sync import sync_file
from wigle_client import WigleUnreachable


def test_has_observations(tmp_path):
    header = "WigleWifi-1.4,appRelease=Kismet\nMAC,SSID,AuthMode,FirstSeen\n"
    header_only = tmp_path / "a.wiglecsv"
    header_only.write_text(header)
    with_data = tmp_path / "b.wiglecsv"
    with_data.write_text(header + "aa:bb:cc:dd:ee:ff,home,[WPA2],2026-09-22\n")
    empty_db = tmp_path / "c.kismet"
    empty_db.touch()

    assert not has_observations(header_only)
    assert has_observations(with_data)
    assert not has_observations(empty_db)


def test_recover_kismet_db_rolls_back_hot_journal(tmp_path):
    db = tmp_path / "Kismet-1.kismet"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE devices (mac TEXT)")
    conn.execute("INSERT INTO devices VALUES ('committed')")
    conn.commit()

    # Simulate Kismet dying mid-transaction: snapshot db + hot journal while a write is in flight.
    conn.execute("PRAGMA cache_size=1")
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO devices VALUES (?)", [("uncommitted" * 50,)] * 500)
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    shutil.copy(db, crashed / db.name)
    shutil.copy(f"{db}-journal", crashed / f"{db.name}-journal")
    conn.rollback()
    conn.close()

    recover_kismet_db(crashed / db.name)

    rows = sqlite3.connect(crashed / db.name).execute("SELECT mac FROM devices").fetchall()
    assert rows == [("committed",)]


def test_pi_is_online():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert pi_is_online("127.0.0.1", port, timeout=1)
    assert not pi_is_online("127.0.0.1", port, timeout=1)
    # PI_HOST is a name: away from home it may not resolve at all.
    assert not pi_is_online("brick69.invalid", port, timeout=1)


def test_wigle_unreachable_defers_the_rest_of_the_run():
    files = [mock.Mock(name=f"f{i}") for i in range(3)]
    stats = RunStats()
    with (
        mock.patch("sync.pi_is_online", return_value=True),
        mock.patch("sync.WigleClient"),
        mock.patch("sync.PiClient") as pi_client,
        mock.patch("sync.sync_file", side_effect=[None, WigleUnreachable("no internet"), None]) as sync_file,
    ):
        pi_client.return_value.list_ready_files.return_value = files
        run(_settings(), stats)

    assert sync_file.call_count == 2  # stopped at the first unreachable upload
    assert stats.deferred == 2
    assert stats.failed == 0
    assert stats.succeeded


def test_wigle_unreachable_leaves_the_next_pis_files_alone():
    files = [mock.Mock(name=f"f{i}") for i in range(2)]
    stats = RunStats()
    with (
        mock.patch("sync.pi_is_online", return_value=True),
        mock.patch("sync.WigleClient"),
        mock.patch("sync.PiClient") as pi_client,
        mock.patch("sync.sync_file", side_effect=WigleUnreachable("no internet")) as sync_file,
    ):
        pi_client.return_value.list_ready_files.return_value = files
        run(_settings(pi_hosts=("brick69", "scootypuffsr")), stats)

    assert sync_file.call_count == 1
    assert stats.by_pi["brick69"].deferred == 2 and stats.by_pi["scootypuffsr"].deferred == 2
    assert stats.deferred == 4 and stats.succeeded


def test_every_pi_is_synced_and_counted_on_its_own():
    online = {"brick69": True, "scootypuffsr": False}
    stats = RunStats()
    with (
        mock.patch("sync.pi_is_online", side_effect=lambda host, port, timeout: online[host]),
        mock.patch("sync.WigleClient"),
        mock.patch("sync.PiClient") as pi_client,
        mock.patch("sync.ledgers.open_ledger"),
        mock.patch("sync.sync_file") as sync_file,
    ):
        pi_client.return_value.list_ready_files.return_value = [mock.Mock(name="f")]
        sync_file.side_effect = lambda pi, wigle, f, settings, stats, ledger: setattr(stats, "uploaded", 1)
        run(_settings(pi_hosts=("brick69", "scootypuffsr")), stats)

    assert [c.args[0].pi_host for c in pi_client.call_args_list] == ["brick69"]
    assert stats.by_pi["brick69"].pi_online and stats.by_pi["brick69"].uploaded == 1
    assert not stats.by_pi["scootypuffsr"].pi_online
    assert stats.pi_online and stats.uploaded == 1 and stats.files_found == 1


def test_one_pis_failure_doesnt_stop_the_next():
    stats = RunStats()
    with (
        mock.patch("sync.pi_is_online", return_value=True),
        mock.patch("sync.WigleClient"),
        mock.patch("sync.PiClient") as pi_client,
        mock.patch("sync.ledgers.open_ledger"),
    ):
        pi_client.return_value.connect.side_effect = [ValueError("bad key"), None]
        pi_client.return_value.list_ready_files.return_value = []
        run(_settings(pi_hosts=("brick69", "scootypuffsr")), stats)

    assert stats.by_pi["brick69"].failed == 1 and stats.by_pi["scootypuffsr"].failed == 0
    assert stats.by_pi["scootypuffsr"].pi_online and not stats.succeeded


def test_settings_hosts():
    assert _settings().hosts == ("pi",)
    assert _settings(pi_hosts=("a", "b")).hosts == ("a", "b")


def test_settings_from_env_reads_pi_hosts(monkeypatch):
    monkeypatch.setenv("WIGLE_API_NAME", "n")
    monkeypatch.setenv("WIGLE_API_TOKEN", "t")
    monkeypatch.setenv("PI_HOSTS", "brick69, scootypuffsr,")
    monkeypatch.setenv("PI_HOST", "ignored")
    settings = Settings.from_env()
    assert settings.hosts == ("brick69", "scootypuffsr") and settings.pi_host == "brick69"
    monkeypatch.delenv("PI_HOSTS")
    assert Settings.from_env().hosts == ("ignored",)


def test_run_stats_add_and_as_dict():
    run_stats = RunStats()
    a, b = RunStats(pi_online=True, uploaded=2, bytes_uploaded=10), RunStats(failed=1, uploaded=1)
    a.detections.update({"flock": 1})
    run_stats.by_pi = {"a": a, "b": b}
    run_stats.add(a)
    run_stats.add(b)
    assert run_stats.pi_online and run_stats.uploaded == 3 and run_stats.failed == 1
    assert run_stats.detections == {"flock": 1}
    out = run_stats.as_dict()
    assert out["by_pi"]["b"]["failed"] == 1 and out["detections"] == {"flock": 1}
    assert "by_pi" not in out["by_pi"]["a"]


def _settings(**over):
    base = dict(
        wigle_api_name="n",
        wigle_api_token="t",
        wigle_donate=False,
        pi_host="pi",
        pi_port=22,
        pi_user="u",
        pi_ssh_key_path="k",
        pi_known_hosts_path=None,
        pi_remote_dir="/r",
        pi_archive_dir="/r/uploaded",
        pi_connect_timeout=1.0,
        pi_probe_timeout=1.0,
        min_file_age_seconds=300,
        file_extensions=(".kismet", ".wiglecsv"),
        compress=False,
    )
    return Settings(**{**base, **over})


def test_kismet_dbs_are_archived_on_the_pi_without_download_or_upload():
    pi, wigle, stats = mock.Mock(), mock.Mock(), RunStats()
    db = RemoteFile("Kismet-1.kismet", "/r/Kismet-1.kismet", 150_000_000, 0, journal_name="Kismet-1.kismet-journal")
    sync_file(pi, wigle, db, _settings(), stats)
    pi.download.assert_not_called()
    wigle.upload.assert_not_called()
    pi.archive.assert_called_once_with(db)
    assert (stats.archived_kept, stats.uploaded) == (1, 0)


def test_wiglecsv_is_still_uploaded(tmp_path):
    csv = tmp_path / "Kismet-1.wiglecsv"
    csv.write_text("WigleWifi-1.4\nMAC,SSID\naa:bb:cc:dd:ee:ff,x\n")
    pi, wigle, stats = mock.Mock(), mock.Mock(), RunStats()
    pi.download.return_value = csv
    wigle.upload.return_value = {"results": []}
    remote = RemoteFile("Kismet-1.wiglecsv", "/r/Kismet-1.wiglecsv", csv.stat().st_size, 0)
    sync_file(pi, wigle, remote, _settings(), stats)
    wigle.upload.assert_called_once()
    pi.archive.assert_called_once_with(remote)
    assert (stats.uploaded, stats.archived_kept) == (1, 0)


def test_span_attributes_are_plain_values():
    # A span drops a Counter with a warning ("Invalid type Counter"), as the
    # 0.1.9 trial showed: detections go one attribute per kind instead.
    stats = RunStats(uploaded=2)
    stats.detections.update({"flock": 3})
    attrs = span_attributes(stats)
    assert attrs["sync.uploaded"] == 2 and attrs["sync.detections.flock"] == 3
    assert "sync.detections" not in attrs
    assert all(isinstance(v, (bool, str, bytes, int, float)) for v in attrs.values())


def _wiglecsv(tmp_path, fixes, name="Kismet-1.wiglecsv"):
    header = (
        "WigleWifi-1.4,appRelease=Kismet\n"
        "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
    )
    rows = "".join(
        f"aa:00:00:00:00:{i:02x},x,[ESS],2026-10-23 00:00:{i % 60:02d},6,-60,{lat},{lon},0,0,WIFI\n"
        for i, (lat, lon) in enumerate(fixes)
    )
    path = tmp_path / name
    path.write_text(header + rows)
    return path


def test_a_session_that_never_moved_is_stationary(tmp_path):
    # GPS wandering indoors: tens of metres around one spot (made-up coordinates).
    assert stationary(_wiglecsv(tmp_path, [(40.0, -75.0), (40.0003, -75.0004), (39.9998, -75.0002)]))


def test_a_drive_is_not_stationary(tmp_path):
    assert not stationary(_wiglecsv(tmp_path, [(40.0, -75.0), (40.0, -75.0), (40.01, -75.0)]))


def test_no_fixes_is_not_judged(tmp_path):
    assert not stationary(_wiglecsv(tmp_path, [(0.0, 0.0)]))
    db = tmp_path / "Kismet-1.kismet"
    db.write_bytes(b"SQLite format 3\0")
    assert not stationary(db)


def test_stationary_sessions_are_archived_without_upload(tmp_path):
    csv = _wiglecsv(tmp_path, [(40.0, -75.0), (40.0001, -75.0001)])
    pi, wigle, stats = mock.Mock(), mock.Mock(), RunStats()
    pi.download.return_value = csv
    remote = RemoteFile(csv.name, f"/r/{csv.name}", csv.stat().st_size, 0)
    sync_file(pi, wigle, remote, _settings(), stats)
    wigle.upload.assert_not_called()
    pi.archive.assert_called_once_with(remote)
    assert (stats.archived_stationary, stats.uploaded) == (1, 0)
