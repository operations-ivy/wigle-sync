"""wigle-sync end to end: a real SSH/SFTP server standing in for the Pi
(tests/fake_pi.py) and a stub WiGLE, with sync.run() doing everything as the
CronJob does: probe, connect with a pinned host key, list what's ready by the
Pi's own clock, download, upload, archive."""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import paramiko
import pytest
from prometheus_client.parser import text_string_to_metric_families

import sync
import wigle_client
from config import Settings
from tests.fake_pi import FakePi, FakeWigle
from observability import RunStats

HEADER = "WigleWifi-1.4,appRelease=Kismet\nMAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude\n"
# A drive: two sightings about 1 km apart (a session that never moves is never uploaded, #26).
ROW = (
    "aa:bb:cc:dd:ee:ff,test-net,[WPA2],2026-10-09 21:15:02,6,-70,40.0,-75.0\n"
    "aa:bb:cc:dd:ee:fe,test-net-2,[WPA2],2026-10-09 21:16:02,6,-70,40.01,-75.0\n"
)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "pi"
    (root / "kismet").mkdir(parents=True)
    key = paramiko.RSAKey.generate(2048)
    key_path = tmp_path / "id_rsa"
    key.write_private_key_file(str(key_path))
    pi, wigle = FakePi(root, key), FakeWigle()
    known = tmp_path / "known_hosts"
    known.write_text(pi.known_hosts_line())
    settings = Settings(
        wigle_api_name="n",
        wigle_api_token="t",
        wigle_donate=False,
        pi_host="127.0.0.1",
        pi_port=pi.port,
        pi_user="wigle-sync",
        pi_ssh_key_path=str(key_path),
        pi_known_hosts_path=str(known),
        pi_remote_dir="/kismet",
        pi_archive_dir="/kismet/uploaded",
        pi_connect_timeout=5.0,
        pi_probe_timeout=2.0,
        min_file_age_seconds=300,
        file_extensions=(".kismet", ".wiglecsv"),
        compress=True,
    )
    with mock.patch.object(wigle_client, "WIGLE_API_URL", wigle.url):
        yield root / "kismet", settings, wigle
    pi.close()
    wigle.close()


def put(kismet_dir, name, content, age_seconds=3600):
    p = kismet_dir / name
    p.write_bytes(content.encode() if isinstance(content, str) else content)
    old = time.time() - age_seconds
    os.utime(p, (old, old))


def archived(kismet_dir):
    up = kismet_dir / "uploaded"
    return sorted(os.listdir(up)) if up.exists() else []


def test_a_drive_is_uploaded_as_wiglecsv_and_everything_else_archived(world):
    kismet, settings, wigle = world
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    put(kismet, "Kismet-A.kismet", b"SQLite format 3\0" + b"x" * 4096)
    put(kismet, "Kismet-B.wiglecsv", HEADER)  # an empty session
    put(kismet, "Kismet-C.wiglecsv", HEADER + ROW, age_seconds=10)  # still being written
    stats = RunStats()
    sync.run(settings, stats)

    assert stats.pi_online and stats.succeeded
    assert (stats.uploaded, stats.archived_kept, stats.archived_empty) == (1, 1, 1)
    # Only the .wiglecsv went to WiGLE, tar.gz'd, with the observation inside.
    [(name, body)] = wigle.uploads
    assert name == "Kismet-A.wiglecsv.tar.gz"
    gz = body[body.index(b"\x1f\x8b") :]
    with tarfile.open(fileobj=io.BytesIO(gz), mode="r:gz") as tar:
        assert ROW.encode() in tar.extractfile("Kismet-A.wiglecsv").read()
    assert archived(kismet) == ["Kismet-A.kismet", "Kismet-A.wiglecsv", "Kismet-B.wiglecsv"]
    assert sorted(f for f in os.listdir(kismet) if f.startswith("Kismet")) == ["Kismet-C.wiglecsv"]


def test_surveillance_devices_on_a_drive_are_counted(world):
    kismet, settings, wigle = world
    flock = "12:34:56:78:9a:bc,Flock-A1B2C3,[WPA2],2026-10-09 21:15:02,6,-70,40.0,-75.0\n"
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW + flock)
    stats = RunStats()
    sync.run(settings, stats)
    assert stats.uploaded == 1 and stats.detections == {"flock": 1}
    [(name, body)] = wigle.uploads
    gz = body[body.index(b"\x1f\x8b") :]
    with tarfile.open(fileobj=io.BytesIO(gz), mode="r:gz") as tar:
        assert flock.encode() in tar.extractfile("Kismet-A.wiglecsv").read()  # still uploaded as it was


def test_a_rejected_upload_stays_on_the_pi_for_the_next_run(world):
    kismet, settings, wigle = world
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    wigle.status = 500
    stats = RunStats()
    sync.run(settings, stats)
    assert (stats.failed, stats.uploaded) == (1, 0)
    assert archived(kismet) == []
    assert (kismet / "Kismet-A.wiglecsv").exists()


def test_wigle_unreachable_defers_without_failing(world):
    kismet, settings, wigle = world
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    put(kismet, "Kismet-B.wiglecsv", HEADER + ROW)
    wigle.close()  # the internet is down
    stats = RunStats()
    sync.run(settings, stats)
    assert stats.deferred == 2 and stats.failed == 0 and stats.succeeded
    assert archived(kismet) == []


def test_a_changed_host_key_is_refused(world, tmp_path):
    kismet, settings, wigle = world
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    other = paramiko.RSAKey.generate(2048)
    (tmp_path / "known_hosts").write_text(f"[127.0.0.1]:{settings.pi_port} ssh-rsa {other.get_base64()}\n")
    stats = RunStats()
    with pytest.raises(paramiko.SSHException):
        sync.run(settings, stats)
    assert wigle.uploads == [] and archived(kismet) == []


def test_pi_away_is_a_quiet_success(world):
    _, settings, wigle = world
    away = Settings(**{**settings.__dict__, "pi_port": 1})  # nothing listens there
    stats = RunStats()
    sync.run(away, stats)
    assert not stats.pi_online and stats.succeeded and wigle.uploads == []


def _crashed_kismet_db(tmp_path):
    """A .kismet and its hot -journal as Kismet leaves them when the power is
    pulled mid-write: one committed row, a transaction in flight."""
    db = tmp_path / "live.kismet"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE devices (mac TEXT)")
    conn.execute("INSERT INTO devices VALUES ('committed')")
    conn.commit()
    conn.execute("PRAGMA cache_size=1")
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO devices VALUES (?)", [("uncommitted" * 50,)] * 500)
    crashed = tmp_path / "crashed"
    crashed.mkdir()
    shutil.copy(db, crashed / "Kismet-J.kismet")
    shutil.copy(f"{db}-journal", crashed / "Kismet-J.kismet-journal")
    conn.rollback()
    conn.close()
    return crashed / "Kismet-J.kismet", crashed / "Kismet-J.kismet-journal"


def test_a_kismet_db_cut_off_mid_write_is_uploaded_rolled_back(world, tmp_path):
    kismet, settings, wigle = world
    db, journal = _crashed_kismet_db(tmp_path)
    put(kismet, db.name, db.read_bytes())
    put(kismet, journal.name, journal.read_bytes())
    with_db = Settings(**{**settings.__dict__, "upload_extensions": (".kismet", ".wiglecsv")})

    stats = RunStats()
    sync.run(with_db, stats)

    assert stats.uploaded == 1 and stats.failed == 0
    [(_, body)] = wigle.uploads
    tar_bytes = body[body.index(b"\x1f\x8b") :]  # the gzip inside the multipart body
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        uploaded = tmp_path / "uploaded.kismet"
        uploaded.write_bytes(tar.extractfile("Kismet-J.kismet").read())
    assert sqlite3.connect(uploaded).execute("SELECT mac FROM devices").fetchall() == [("committed",)]
    # The db and its journal go to uploaded/ together.
    assert archived(kismet) == ["Kismet-J.kismet", "Kismet-J.kismet-journal"]


def test_pushed_metrics_read_as_prometheus_reads_them(world):
    """The CronJob's push, as the Pushgateway stores it and Prometheus scrapes it."""
    kismet, settings, _ = world
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    pushes = []

    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):  # the totals, as the gateway would hold them
            body = b'wigle_sync_files_uploaded_since_launch{job="wigle-sync"} 10\n'
            body += b'wigle_sync_bytes_uploaded_since_launch{job="wigle-sync"} 1000\n'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            pushes.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stats = RunStats()
    try:
        with mock.patch.dict(os.environ, {"PUSHGATEWAY_URL": f"http://127.0.0.1:{server.server_address[1]}"}):
            sync.run(settings, stats)
            sync.push_metrics(stats, finished_at=1000.0)
    finally:
        server.shutdown()
        server.server_close()

    [(path, body)] = pushes
    assert path == "/metrics/job/wigle-sync"
    samples = {s.name: s.value for f in text_string_to_metric_families(body.decode()) for s in f.samples}
    assert samples["wigle_sync_files_uploaded"] == 1
    assert samples["wigle_sync_files_uploaded_since_launch"] == 11
    assert samples["wigle_sync_bytes_uploaded_since_launch"] == 1000 + stats.bytes_uploaded
    assert samples["wigle_sync_last_success_timestamp_seconds"] == 1000.0
    assert samples["wigle_sync_pi_online"] == 1
