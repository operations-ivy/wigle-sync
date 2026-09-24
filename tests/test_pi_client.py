from __future__ import annotations

import stat
from unittest import mock

import paramiko

from pi_client import PiClient
from pi_client import select_ready_files

NOW = 1_700_000_000


def _entry(name: str, age: int, size: int = 100, mode: int = stat.S_IFREG | 0o644) -> paramiko.SFTPAttributes:
    attrs = paramiko.SFTPAttributes()
    attrs.filename = name
    attrs.st_mtime = NOW - age
    attrs.st_size = size
    attrs.st_mode = mode
    return attrs


def test_selects_only_settled_capture_files_oldest_first():
    entries = [
        _entry("Kismet-2.kismet", age=600),
        _entry("Kismet-1.wiglecsv", age=3600),
        _entry("Kismet-live.kismet", age=10),
        _entry("Kismet-2.kismet-journal", age=600),
        _entry("notes.txt", age=600),
        _entry("uploaded", age=600, mode=stat.S_IFDIR | 0o755),
    ]

    ready = select_ready_files(entries, "/var/log/kismet", (".kismet", ".wiglecsv"), 300, NOW)

    assert [f.name for f in ready] == ["Kismet-1.wiglecsv", "Kismet-2.kismet"]
    assert ready[1].path == "/var/log/kismet/Kismet-2.kismet"


def test_pairs_kismet_db_with_its_journal_and_waits_on_journal_mtime():
    entries = [
        _entry("Kismet-1.kismet", age=600),
        _entry("Kismet-1.kismet-journal", age=600),
        _entry("Kismet-2.kismet", age=600),
        _entry("Kismet-2.kismet-journal", age=10),
    ]

    ready = select_ready_files(entries, "/var/log/kismet", (".kismet",), 300, NOW)

    assert [(f.name, f.journal_name) for f in ready] == [("Kismet-1.kismet", "Kismet-1.kismet-journal")]


def test_file_age_is_measured_against_the_pis_clock_not_ours():
    # Pi just booted with a stale clock 3h behind: Kismet's live file was
    # written "now" by the Pi's clock, which is 3h ago by ours.
    pi_clock = NOW - 3 * 3600
    live = _entry("Kismet-live.kismet", age=0)
    live.st_mtime = pi_clock

    settings = mock.Mock(pi_remote_dir="/var/log/kismet", file_extensions=(".kismet",), min_file_age_seconds=300)
    client = PiClient(settings)
    client._sftp = mock.MagicMock()
    client._sftp.listdir_attr.return_value = [live]
    client._sftp.stat.return_value = mock.Mock(st_mtime=pi_clock)

    assert client.list_ready_files() == []


def test_idle_wiglecsv_waits_while_its_session_db_is_still_being_written():
    # Parked with no GPS fix: the live .wiglecsv is untouched (header only) for
    # hours while Kismet keeps writing the session's .kismet db.
    entries = [
        _entry("Kismet-live-1.wiglecsv", age=3 * 3600, size=254),
        _entry("Kismet-live-1.kismet", age=5),
        _entry("Kismet-old-1.wiglecsv", age=3600),
        _entry("Kismet-old-1.kismet", age=3600),
    ]

    ready = select_ready_files(entries, "/var/log/kismet", (".kismet", ".wiglecsv"), 300, NOW)

    assert sorted(f.name for f in ready) == ["Kismet-old-1.kismet", "Kismet-old-1.wiglecsv"]
