"""pi/handheld_common.py: chrony's sync state and atomic writes."""

from __future__ import annotations

import pytest
from handheld_common import parse_chrony_synced
from handheld_common import write_atomic

import tests.conftest  # noqa: F401 (puts pi/ on sys.path)


@pytest.mark.parametrize(
    "csv_text, synced",
    [
        ("CB007105,203.0.113.5,3,1763294400.0,0.0001,0.0,0.0,1.0,0.0,0.0,0.01,0.001,64.0,Normal\n", True),
        ("00000000,,0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,Not synchronised\n", False),
        ("7F7F0101,,10,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,Normal\n", False),
        ("", False),
    ],
    ids=["synced", "no source", "local clock only", "no output"],
)
def test_parse_chrony_synced(csv_text, synced):
    assert parse_chrony_synced(csv_text) is synced


def test_write_atomic(tmp_path):
    target = tmp_path / "sub" / "pi.prom"
    write_atomic(target, "a 1\n")
    write_atomic(target, "a 2\n")
    assert target.read_text() == "a 2\n"
    assert [p.name for p in target.parent.iterdir()] == ["pi.prom"]


def test_write_atomic_leaves_no_temp_file_on_failure(tmp_path):
    target = tmp_path / "taken"
    target.mkdir()  # os.replace can't put a file over a directory
    with pytest.raises(OSError):
        write_atomic(target, "x\n")
    assert list(tmp_path.iterdir()) == [target]
