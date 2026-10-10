"""wigle-sync end to end: a real SSH/SFTP server standing in for the Pi
(tests/fake_pi.py) and a stub WiGLE, with sync.run() doing everything as the
CronJob does: probe, connect with a pinned host key, list what's ready by the
Pi's own clock, download, upload, archive."""

from __future__ import annotations

import io
import os
import tarfile
import time
from unittest import mock

import paramiko
import pytest

import sync
import wigle_client
from config import Settings
from tests.fake_pi import FakePi, FakeWigle
from observability import RunStats

HEADER = "WigleWifi-1.4,appRelease=Kismet\nMAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude\n"
ROW = "aa:bb:cc:dd:ee:ff,test-net,[WPA2],2026-10-09 21:15:02,6,-70,40.0,-75.0\n"


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
        wigle_api_name="n", wigle_api_token="t", wigle_donate=False, pi_host="127.0.0.1", pi_port=pi.port,
        pi_user="wigle-sync", pi_ssh_key_path=str(key_path), pi_known_hosts_path=str(known),
        pi_remote_dir="/kismet", pi_archive_dir="/kismet/uploaded", pi_connect_timeout=5.0, pi_probe_timeout=2.0,
        min_file_age_seconds=300, file_extensions=(".kismet", ".wiglecsv"), compress=True)
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
    gz = body[body.index(b"\x1f\x8b"):]
    with tarfile.open(fileobj=io.BytesIO(gz), mode="r:gz") as tar:
        assert ROW.encode() in tar.extractfile("Kismet-A.wiglecsv").read()
    assert archived(kismet) == ["Kismet-A.kismet", "Kismet-A.wiglecsv", "Kismet-B.wiglecsv"]
    assert sorted(f for f in os.listdir(kismet) if f.startswith("Kismet")) == ["Kismet-C.wiglecsv"]


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
