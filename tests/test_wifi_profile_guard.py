"""pi/rootfs/usr/local/sbin/wifi-profile-guard (#25), run against temp
directories and a stand-in nmcli. The profile here is made up."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pi" / "rootfs" / "usr" / "local" / "sbin" / "wifi-profile-guard"
PROFILE = "[connection]\nid=home-wifi\ntype=wifi\n\n[wifi-security]\npsk=not-a-real-password\n"


def run(tmp_path, nm_running=True):
    calls = tmp_path / "nmcli-calls"
    nmcli = tmp_path / "nmcli"
    nmcli.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n[ "$1" = -t ] && exit {0 if nm_running else 1}\nexit 0\n')
    nmcli.chmod(0o755)
    env = {
        "PATH": "/usr/bin:/bin",
        "PROFILES": str(tmp_path / "connections"),
        "BACKUP": str(tmp_path / "backup"),
        "NETPLAN": str(tmp_path / "netplan"),
        "NMCLI": str(nmcli),
    }
    for d in ("connections", "netplan"):
        (tmp_path / d).mkdir(exist_ok=True)
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout, calls.read_text() if calls.exists() else ""


def test_a_good_profile_is_copied_root_only(tmp_path):
    (tmp_path / "connections").mkdir()
    (tmp_path / "connections" / "home-wifi.nmconnection").write_text(PROFILE)
    out, calls = run(tmp_path)
    saved = tmp_path / "backup" / "home-wifi.nmconnection"
    assert saved.read_text() == PROFILE
    assert oct(saved.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "backup").stat().st_mode & 0o777) == "0o700"
    assert "reload" not in calls
    # Nothing changed: the second boot writes nothing.
    assert run(tmp_path)[0] == ""


def test_an_emptied_profile_is_put_back_and_nm_reloads(tmp_path):
    live = tmp_path / "connections" / "home-wifi.nmconnection"
    (tmp_path / "connections").mkdir()
    live.write_text(PROFILE)
    run(tmp_path)
    live.write_text("")  # the power cut
    out, calls = run(tmp_path)
    assert live.read_text() == PROFILE
    assert "restored" in out
    assert "connection reload" in calls
    # The empty file never replaced the good copy.
    assert (tmp_path / "backup" / "home-wifi.nmconnection").read_text() == PROFILE


def test_a_missing_profile_is_put_back_without_nm_running(tmp_path):
    (tmp_path / "connections").mkdir()
    (tmp_path / "connections" / "home-wifi.nmconnection").write_text(PROFILE)
    run(tmp_path)
    (tmp_path / "connections" / "home-wifi.nmconnection").unlink()
    out, calls = run(tmp_path, nm_running=False)
    assert (tmp_path / "connections" / "home-wifi.nmconnection").read_text() == PROFILE
    assert "connection reload" not in calls


def test_garbage_is_not_saved_over_the_good_copy(tmp_path):
    live = tmp_path / "connections" / "home-wifi.nmconnection"
    (tmp_path / "connections").mkdir()
    live.write_text(PROFILE)
    run(tmp_path)
    live.write_bytes(b"\0" * 300)  # same failure, NUL-filled instead of empty
    run(tmp_path)
    assert live.read_text() == PROFILE


def test_empty_netplan_files_are_moved_aside(tmp_path):
    (tmp_path / "netplan").mkdir()
    (tmp_path / "netplan" / "90-NM-x.yaml").write_text("")
    (tmp_path / "netplan" / "50-ok.yaml").write_text("network: {version: 2}\n")
    out, _ = run(tmp_path)
    assert sorted(p.name for p in (tmp_path / "netplan").iterdir()) == ["50-ok.yaml"]
    assert (tmp_path / "backup" / "netplan-empty" / "90-NM-x.yaml").exists()
