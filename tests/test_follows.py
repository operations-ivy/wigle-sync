"""follows.py (#83): which devices count as following, from made-up drives
(the coordinates are invented, around 40, -75), and the sync publishing them
to an in-memory stand-in for Kafka (the real topics are checked on the cluster)."""

from __future__ import annotations

from unittest import mock

import follows
import ledger as ledgers
import sync
from observability import RunStats
from tests.test_functional_sync import put
from tests.test_functional_sync import world  # noqa: F401 (fixture)
from tests.test_ledger import MemoryLedger

HEADER = (
    "WigleWifi-1.4,appRelease=Kismet\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
)
HOME = (40.0, -75.0)
TRACKER = "0C:11:22:33:44:55"  # maker-assigned: 0x0C has the local bit clear
RANDOM = "DA:11:22:33:44:55"  # random: 0xDA has it set
DAY = 86_400


def row(mac, when, lat, lon, kind="BLE", name=""):
    return f"{mac},{name},Misc [LE],{when},0,0,{lat},{lon},0,0,{kind}\n"


def drive_text(day, place, macs=(TRACKER, RANDOM)):
    """A drive on 2026-11-<day>: leaves home at 08:00, heard everything at `place` at 08:20."""
    rows = [row("00:AA:00:00:00:01", f"2026-11-{day:02d} 08:00:00", *HOME, kind="BT", name="neighbour")]
    rows += [row(mac, f"2026-11-{day:02d} 08:20:00", *place) for mac in macs]
    rows.append(row(TRACKER if TRACKER in macs else macs[0], f"2026-11-{day:02d} 08:30:00", place[0] + 0.01, place[1]))
    return HEADER + "".join(rows)


def history(*drives):
    h = follows.History()
    for i, text in enumerate(drives):
        for s in follows.sightings(text, f"drive{i}"):
            h.add(s)
    return h


def test_a_drive_gives_each_fixed_bluetooth_address_once_with_its_first_and_last_fix():
    found = {s.mac: s for s in follows.sightings(drive_text(1, (40.1, -75.0)), "d1")}
    assert set(found) == {TRACKER, "00:AA:00:00:00:01"}  # the random address is never followed
    tracker = found[TRACKER]
    assert (tracker.first.lat, tracker.last.lat) == (40.1, 40.11)
    assert tracker.last.t - tracker.first.t == 600
    assert tracker.key == f"{TRACKER}|d1"


def test_wifi_and_rows_without_a_fix_are_not_sightings():
    text = HEADER + row("0C:00:00:00:00:01", "2026-11-01 08:00:00", 40.1, -75.0, kind="WIFI")
    text += row("0C:00:00:00:00:02", "2026-11-01 08:00:00", 0, 0)
    assert follows.sightings(text, "d") == []


def test_a_sighting_survives_its_kafka_record():
    [s, *_] = follows.sightings(drive_text(1, (40.1, -75.0)), "d1")
    assert follows.Sighting.from_record(s.record()) == s


def test_heard_on_three_trips_in_different_places_is_a_candidate():
    h = history(*(drive_text(day, (40.0 + 0.05 * day, -75.0)) for day in (1, 2, 3)))
    [c] = h.candidates(follows.Rules(home=HOME))
    assert c["mac"] == TRACKER and c["trips"] == 3 and c["spread_km"] > 2
    assert [len(seen["places"]) for seen in c["seen"]] == [2, 2, 2]


def test_two_trips_are_not_enough():
    h = history(*(drive_text(day, (40.0 + 0.05 * day, -75.0)) for day in (1, 2)))
    assert h.candidates(follows.Rules(home=HOME)) == []


def test_a_device_always_in_the_same_place_is_not_following():
    # A shop's beacon on the way to work: heard every day, always there.
    h = history(*(drive_text(day, (40.2, -75.0)) for day in (1, 2, 3, 4)))
    assert h.candidates(follows.Rules(home=HOME, min_km=2.0)) == []


def test_sightings_at_home_dont_count():
    # The neighbour's speaker: heard as every drive leaves.
    h = history(*(drive_text(day, (40.0 + 0.05 * day, -75.0)) for day in (1, 2, 3)))
    assert "00:AA:00:00:00:01" not in {c["mac"] for c in h.candidates(follows.Rules(home=HOME))}


def test_home_is_where_drives_start_when_not_given():
    h = history(*(drive_text(day, (40.0 + 0.05 * day, -75.0)) for day in (1, 2, 3)))
    assert h.home() == HOME
    assert [c["mac"] for c in h.candidates(follows.Rules())] == [TRACKER]


def test_sessions_close_together_are_one_trip():
    # Two Kismet sessions (or both Pis) on one outing, then a second outing: two trips.
    one = drive_text(1, (40.1, -75.0))
    again = one.replace("08:", "09:")
    later = drive_text(5, (40.3, -75.0))
    h = history(one, again, later)
    assert h.candidates(follows.Rules(home=HOME, min_trips=3)) == []
    assert h.candidates(follows.Rules(home=HOME, min_trips=2))[0]["trips"] == 2


def test_allowed_and_own_addresses_are_never_candidates():
    h = history(*(drive_text(day, (40.0 + 0.05 * day, -75.0)) for day in (1, 2, 3)))
    assert h.candidates(follows.Rules(home=HOME, allow=frozenset({TRACKER}))) == []
    assert h.candidates(follows.Rules(home=HOME), own=frozenset({TRACKER})) == []


def test_a_drive_sent_twice_is_one_sighting():
    h = history(drive_text(1, (40.1, -75.0)))
    for s in follows.sightings(drive_text(1, (40.1, -75.0)), "drive0"):
        h.add(s)
    assert len(h.devices[TRACKER]) == 1
    h.forget(f"{TRACKER}|drive0")
    assert len(h.devices[TRACKER]) == 0


def test_rules_from_the_environment(monkeypatch):
    monkeypatch.setenv("FOLLOWS_MIN_TRIPS", "4")
    monkeypatch.setenv("FOLLOWS_TRIP_GAP_HOURS", "1")
    monkeypatch.setenv("RADAR_CENTER", "40.0,-75.0")
    monkeypatch.setenv("FOLLOWS_ALLOW", "0c:11:22:33:44:55, 0c:00:00:00:00:01\n")
    rules = follows.Rules.from_env()
    assert (rules.min_trips, rules.trip_gap, rules.home) == (4, 3600, HOME)
    assert rules.allow == {TRACKER, "0C:00:00:00:00:01"}
    monkeypatch.setenv("RADAR_CENTER", "nonsense")
    assert follows.Rules.from_env().home is None


class MemoryFollows(follows.NullFollows):
    """What the two topics would hold, in memory: survives between runs like Kafka."""

    available = True

    def __init__(self):
        super().__init__()
        self.published = {}
        self.latest = None

    def publish(self, found):
        for s in found:
            self.history.add(s)
            self.published[s.key] = s.record()

    def publish_candidates(self, candidates, rules):
        self.latest = candidates


def run(settings, memory):
    stats = RunStats()
    with (
        mock.patch.object(follows, "open_follows", return_value=memory),
        mock.patch.object(ledgers, "open_ledger", return_value=MemoryLedger()),
    ):
        sync.run(settings, stats)
    return stats


def test_the_sync_publishes_each_drive_and_tells_the_pi_its_candidates(world, monkeypatch):  # noqa: F811
    kismet, settings, _ = world
    monkeypatch.setenv("FOLLOWS_HOME", "40.0,-75.0")
    memory = MemoryFollows()
    for day in (1, 2):
        put(kismet, f"Kismet-{day}.wiglecsv", drive_text(day, (40.0 + 0.05 * day, -75.0)))
    stats = run(settings, memory)
    assert stats.uploaded == 2 and stats.sightings == 4 and stats.follows_candidates == 0
    assert (kismet / "follows.txt").read_text() == ""

    put(kismet, "Kismet-3.wiglecsv", drive_text(3, (40.15, -75.0)))
    stats = run(settings, memory)
    assert stats.follows_candidates == 1 and [c["mac"] for c in memory.latest] == [TRACKER]
    assert (kismet / "follows.txt").read_text() == f"{TRACKER}\n"
    assert not (kismet / "follows.txt.new").exists()


def test_backfill_publishes_the_drives_already_archived(world, monkeypatch):  # noqa: F811
    kismet, settings, _ = world
    monkeypatch.setenv("FOLLOWS_HOME", "40.0,-75.0")
    (kismet / "uploaded").mkdir()
    for day in (1, 2, 3):
        put(kismet / "uploaded", f"Kismet-{day}.wiglecsv", drive_text(day, (40.0 + 0.05 * day, -75.0)))
    memory = MemoryFollows()
    stats = RunStats()
    with mock.patch.object(follows, "open_follows", return_value=memory):
        assert sync.backfill_sightings(settings, stats) == 0
    assert stats.sightings == 6 and stats.follows_candidates == 1
    assert (kismet / "follows.txt").read_text() == f"{TRACKER}\n"


def test_without_kafka_the_sync_runs_as_before(world):  # noqa: F811
    kismet, settings, _ = world
    put(kismet, "Kismet-1.wiglecsv", drive_text(1, (40.05, -75.0)))
    stats = run(settings, follows.NullFollows())
    assert stats.uploaded == 1 and stats.sightings == 0 and stats.follows_candidates is None
    assert not (kismet / "follows.txt").exists()
