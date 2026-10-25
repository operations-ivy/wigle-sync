"""pi/rootfs/usr/local/sbin/kismet-prune-uploaded (#24), against a temp
uploaded/ and a stand-in for df: free space rises as files go."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "pi" / "rootfs" / "usr" / "local" / "sbin" / "kismet-prune-uploaded"


def run(tmp_path, min_free_mb):
    # "Free space" in MiB: 1000 less one per file left, so it rises as files go.
    free = f"echo $((1000 - $(ls {tmp_path / 'uploaded'} | wc -l)))"
    env = {
        "PATH": "/usr/bin:/bin",
        "DIR": str(tmp_path / "uploaded"),
        "MIN_FREE_MB": str(min_free_mb),
        "FREE_MB_CMD": free,
    }
    result = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return sorted(p.name for p in (tmp_path / "uploaded").iterdir()), result.stdout


def sessions(tmp_path, *bases):
    up = tmp_path / "uploaded"
    up.mkdir(exist_ok=True)
    for b in bases:
        for ext in (".kismet", ".kismet-journal", ".wiglecsv"):
            (up / f"{b}{ext}").write_text("x")


def test_nothing_goes_while_there_is_room(tmp_path):
    sessions(tmp_path, "Kismet-20261024-13-37-10-1", "Kismet-20261025-14-17-34-1")
    left, out = run(tmp_path, min_free_mb=900)
    assert len(left) == 6 and out == ""


def test_the_oldest_session_goes_first_whole(tmp_path):
    # Named for their start: the 2026-10-14 one is oldest whatever the mtimes say.
    sessions(tmp_path, "Kismet-20261025-14-17-34-1", "Kismet-20261014-20-54-03-1", "Kismet-20261024-13-37-10-1")
    left, out = run(tmp_path, min_free_mb=993)  # 9 files: 991 free, need 2 more MiB
    assert not any(n.startswith("Kismet-20261014") for n in left)
    assert len(left) == 6  # one session's three files, all of it
    assert "removing" in out


def test_an_empty_dir_never_loops(tmp_path):
    (tmp_path / "uploaded").mkdir()
    left, out = run(tmp_path, min_free_mb=5000)
    assert left == [] and "nothing left" in out


def test_other_files_are_never_touched(tmp_path):
    sessions(tmp_path, "Kismet-20261014-20-54-03-1")
    (tmp_path / "uploaded" / "notes.txt").write_text("keep me")
    left, _ = run(tmp_path, min_free_mb=5000)
    assert left == ["notes.txt"]
