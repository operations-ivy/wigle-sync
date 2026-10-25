"""ledger.py (#21) and how the sync uses it, with an in-memory ledger in
place of Kafka (the real topic is checked on the cluster)."""

from __future__ import annotations

import shutil
from unittest import mock

import ledger as ledgers
import sync
from observability import RunStats
from tests.test_functional_sync import HEADER
from tests.test_functional_sync import ROW
from tests.test_functional_sync import archived
from tests.test_functional_sync import put
from tests.test_functional_sync import world  # noqa: F401 (fixture)


class MemoryLedger(ledgers.NullLedger):
    """What Kafka would hold, kept in a dict: survives between runs like the topic."""

    available = True

    def __init__(self):
        self.states = {}

    def uploaded(self, key):
        s = self.states.get(key)
        return s if s and s["state"] == "uploaded" else None

    def record(self, key, state, **fields):
        self.states[key] = {"state": state, **fields}


def run(settings, memory):
    stats = RunStats()
    with mock.patch.object(ledgers, "open_ledger", return_value=memory):
        sync.run(settings, stats)
    return stats


def test_the_key_is_the_files_content_not_its_name(tmp_path):
    a, b, c = tmp_path / "a.wiglecsv", tmp_path / "renamed.wiglecsv", tmp_path / "c.wiglecsv"
    a.write_text(HEADER + ROW)
    b.write_text(HEADER + ROW)
    c.write_text(HEADER)
    assert ledgers.file_key(a) == ledgers.file_key(b) != ledgers.file_key(c)


def test_a_session_copied_back_onto_the_pi_is_not_uploaded_again(world):  # noqa: F811
    kismet, settings, wigle = world
    memory = MemoryLedger()
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    assert run(settings, memory).uploaded == 1
    [record] = memory.states.values()
    assert record["state"] == "uploaded" and record["file"] == "Kismet-A.wiglecsv"

    # Copied back under another name, as a restore from a backup might.
    shutil.copy(kismet / "uploaded" / "Kismet-A.wiglecsv", kismet / "Kismet-A-copy.wiglecsv")
    put(kismet, "Kismet-A-copy.wiglecsv", (kismet / "uploaded" / "Kismet-A.wiglecsv").read_text())
    stats = run(settings, memory)
    assert (stats.uploaded, stats.already_uploaded) == (0, 1)
    assert len(wigle.uploads) == 1
    assert "Kismet-A-copy.wiglecsv" in archived(kismet)


def test_wiping_uploaded_dir_causes_no_reupload(world):  # noqa: F811
    kismet, settings, wigle = world
    memory = MemoryLedger()
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    run(settings, memory)
    content = (kismet / "uploaded" / "Kismet-A.wiglecsv").read_text()
    shutil.rmtree(kismet / "uploaded")
    put(kismet, "Kismet-A.wiglecsv", content)  # back in the log dir, uploaded/ gone
    stats = run(settings, memory)
    assert stats.already_uploaded == 1 and len(wigle.uploads) == 1


def test_no_ledger_is_the_old_behaviour(world):  # noqa: F811
    kismet, settings, wigle = world
    put(kismet, "Kismet-A.wiglecsv", HEADER + ROW)
    stats = RunStats()
    with mock.patch.dict("os.environ", {}, clear=False):
        sync.run(settings, stats)  # KAFKA_BOOTSTRAP unset
    assert stats.uploaded == 1


def test_an_unreachable_kafka_falls_back_to_no_ledger():
    with mock.patch.object(ledgers.Ledger, "load", side_effect=ledgers.KafkaException("down")):
        assert isinstance(ledgers.open_ledger("kafka:9092"), ledgers.NullLedger)
        assert not ledgers.open_ledger("kafka:9092").available
    assert not ledgers.open_ledger(None).available
