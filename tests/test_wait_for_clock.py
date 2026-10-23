"""pi/rootfs/usr/local/sbin/wait-for-clock, run against a stand-in chronyc."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pi" / "rootfs" / "usr" / "local" / "sbin" / "wait-for-clock"


def run(tmp_path, chronyc_exit):
    args = tmp_path / "args"
    chronyc = tmp_path / "chronyc"
    chronyc.write_text(f'#!/bin/sh\necho "$@" > {args}\nexit {chronyc_exit}\n')
    chronyc.chmod(0o755)
    out = tmp_path / "textfile" / "clock.prom"
    env = {"PATH": "/usr/bin:/bin", "CHRONYC": str(chronyc), "OUT": str(out), "LIMIT": "180"}
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    return result, out.read_text(), args.read_text().split()


def test_synced(tmp_path):
    result, prom, args = run(tmp_path, 0)
    assert result.returncode == 0
    assert "pi_clock_synced_at_boot 1\n" in prom
    assert "pi_clock_wait_seconds 0\n" in prom
    assert "synced=1" in result.stdout


def test_limit_reached_still_lets_kismet_start(tmp_path):
    result, prom, _ = run(tmp_path, 1)
    assert result.returncode == 0
    assert "pi_clock_synced_at_boot 0\n" in prom


def test_asks_chrony_for_the_limit_in_one_second_tries(tmp_path):
    _, _, args = run(tmp_path, 0)
    assert args == ["-h", "127.0.0.1,::1", "waitsync", "180", "1", "0", "1"]


def test_no_temporary_file_left(tmp_path):
    run(tmp_path, 0)
    assert sorted(p.name for p in (tmp_path / "textfile").iterdir()) == ["clock.prom"]
