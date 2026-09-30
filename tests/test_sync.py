from __future__ import annotations

import shutil
import socket
import sqlite3
from unittest import mock

from observability import RunStats
from sync import has_observations
from sync import pi_is_online
from sync import recover_kismet_db
from sync import run
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
        run(mock.Mock(), stats)

    assert sync_file.call_count == 2  # stopped at the first unreachable upload
    assert stats.deferred == 2
    assert stats.failed == 0
    assert stats.succeeded
