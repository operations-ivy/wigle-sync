"""Follows-me detection (#83): a device heard drive after drive, in different
places, away from home, is how a tracker on the car (or someone following)
looks in the data.

- Sightings: for each drive, every Bluetooth and BLE device with an address
  its maker assigned (detect.globally_administered), with its first and last
  fix. Random addresses (most phones, AirTags) change too often to follow.
  The sync publishes them to wigle-sync.sightings (brick-k8s-config's
  kafka/), one record per device per drive, keyed "<MAC>|<drive>" with the
  drive's ledger key (the SHA-256 of its .wiglecsv), so a drive sent twice
  is one record.
- Candidates: a device heard on Rules.min_trips separate trips (sessions
  within trip_gap of each other are one trip: two Kismet sessions, or two
  Pis, on one outing), at places at least min_km apart once fixes within
  home_km of home are left out. Home is HOME ("lat,lon") or, without it,
  the median of where drives start. Addresses on FOLLOWS_ALLOW (the user's
  own phone, car, Pis) and the Pi's own radios never are.
- The sync publishes the candidate list to wigle-sync.follows (one record,
  for the console's FOLLOWS ME panel) and writes their addresses to
  follows.txt on the Pi (LED2: pi/leds/status-leds.py).

Privacy, as detect.py: logs and metrics get counts only. Addresses, names,
places and times are in the two topics and on the console page, nowhere else.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from typing import Any
from typing import NamedTuple

import brick_observe
from confluent_kafka import Consumer
from confluent_kafka import KafkaException
from confluent_kafka import Producer

import detect
import report
from ledger import KAFKA_LOG
from ledger import read_topic

log = brick_observe.get_logger(__name__)

SIGHTINGS_TOPIC = "wigle-sync.sightings"
FOLLOWS_TOPIC = "wigle-sync.follows"
FOLLOWS_KEY = "candidates"
KINDS = {"BT": "bt", "BLE": "ble"}
READ_TIMEOUT = 120.0  # seconds: the sightings topic holds every drive's devices


class Fix(NamedTuple):
    t: float
    lat: float
    lon: float


class Sighting(NamedTuple):
    """One device on one drive: compact, as the history holds every one."""

    mac: str
    drive: str
    kind: str
    name: str
    first: Fix
    last: Fix

    def record(self) -> dict[str, Any]:
        return {
            "mac": self.mac,
            "drive": self.drive,
            "kind": self.kind,
            "name": self.name,
            "first": list(self.first),
            "last": list(self.last),
        }

    @classmethod
    def from_record(cls, value: dict[str, Any]) -> Sighting:
        return cls(
            value["mac"],
            value["drive"],
            value["kind"],
            value.get("name", ""),
            Fix(*value["first"]),
            Fix(*value["last"]),
        )

    @property
    def key(self) -> str:
        return f"{self.mac}|{self.drive}"


def sightings(text: str, drive: str) -> list[Sighting]:
    """The followable devices in a .wiglecsv's text: Bluetooth and BLE with a
    maker-assigned address and at least one GPS fix."""
    rows, _ = report.parse_rows(text)
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        kind = KINDS.get(row["Type"])
        mac = (row.get("MAC") or "").strip().upper()
        if not kind or not detect.globally_administered(mac):
            continue
        lat, lon = report.to_float(row.get("CurrentLatitude")), report.to_float(row.get("CurrentLongitude"))
        if not lat and not lon:  # Kismet writes 0,0 (or nothing) without a fix
            continue
        fix = Fix(row["_t"], lat, lon)
        seen = found.setdefault(mac, {"kind": kind, "name": "", "first": fix, "last": fix})
        seen["first"], seen["last"] = min(seen["first"], fix), max(seen["last"], fix)
        seen["name"] = seen["name"] or report.decode_name(row.get("SSID"))
    return [Sighting(mac, drive, s["kind"], s["name"], s["first"], s["last"]) for mac, s in sorted(found.items())]


def parse_point(value: str) -> tuple[float, float] | None:
    """ "lat,lon" as a pair, or None when unset or unreadable."""
    try:
        lat, lon = (float(part) for part in value.split(","))
    except ValueError:
        return None
    return lat, lon


def parse_macs(value: str) -> frozenset[str]:
    """Comma, space or newline separated addresses, upper case."""
    return frozenset(m.upper() for m in value.replace(",", " ").split())


@dataclass(frozen=True)
class Rules:
    min_trips: int = 3
    min_km: float = 2.0
    home_km: float = 1.0
    trip_gap: float = 2 * 3600  # seconds between sessions that make them separate trips
    home: tuple[float, float] | None = None
    allow: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> Rules:
        return cls(
            min_trips=int(os.environ.get("FOLLOWS_MIN_TRIPS", cls.min_trips)),
            min_km=float(os.environ.get("FOLLOWS_MIN_KM", cls.min_km)),
            home_km=float(os.environ.get("FOLLOWS_HOME_KM", cls.home_km)),
            trip_gap=float(os.environ.get("FOLLOWS_TRIP_GAP_HOURS", cls.trip_gap / 3600)) * 3600,
            home=parse_point(os.environ.get("FOLLOWS_HOME") or os.environ.get("RADAR_CENTER") or ""),
            allow=parse_macs(os.environ.get("FOLLOWS_ALLOW", "")),
        )


def _km(a: Fix, b: tuple[float, float]) -> float:
    return report.metres((a.lat, a.lon), b) / 1000


class History:
    """Every sighting, by device then drive (a drive sent again replaces itself)."""

    def __init__(self) -> None:
        self.devices: dict[str, dict[str, Sighting]] = defaultdict(dict)

    def add(self, sighting: Sighting) -> None:
        self.devices[sighting.mac][sighting.drive] = sighting

    def forget(self, key: str) -> None:
        mac, _, drive = key.partition("|")
        self.devices.get(mac, {}).pop(drive, None)

    def __len__(self) -> int:
        return sum(len(d) for d in self.devices.values())

    def home(self) -> tuple[float, float] | None:
        """The median of where drives start (each drive's earliest fix): most
        drives start at home."""
        starts: dict[str, Fix] = {}
        for drives in self.devices.values():
            for s in drives.values():
                if s.drive not in starts or s.first.t < starts[s.drive].t:
                    starts[s.drive] = s.first
        if not starts:
            return None
        return statistics.median(f.lat for f in starts.values()), statistics.median(f.lon for f in starts.values())

    def candidates(self, rules: Rules, own: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
        home = rules.home or self.home()
        skip = rules.allow | own
        found = []
        for mac, drives in self.devices.items():
            if mac in skip or len(drives) < rules.min_trips:
                continue
            away = _away(drives.values(), home, rules.home_km)
            trips = _count_trips([s for s, _ in away], rules.trip_gap)
            if trips < rules.min_trips:
                continue
            spread = _spread_km([p for _, ps in away for p in ps])
            if spread < rules.min_km:
                continue
            found.append(_candidate(mac, away, trips, spread))
        return sorted(found, key=lambda c: -c["last_seen"])


Away = list[tuple[Sighting, list[Fix]]]


def _away(sightings: Iterable[Sighting], home: tuple[float, float] | None, home_km: float) -> Away:
    """Each sighting with its fixes more than home_km from home, oldest first;
    sightings only at home are left out."""
    away = [
        (s, points)
        for s in sightings
        if (points := [f for f in (s.first, s.last) if home is None or _km(f, home) > home_km])
    ]
    return sorted(away, key=lambda sp: sp[0].first.t)


def _count_trips(sightings: list[Sighting], gap: float) -> int:
    """Separate trips among sightings sorted by start: one that starts more
    than `gap` seconds after the ones before it ended is a new trip."""
    trips, last_end = 0, float("-inf")
    for s in sightings:
        if s.first.t - last_end > gap:
            trips += 1
        last_end = max(last_end, s.last.t)
    return trips


def _spread_km(points: list[Fix]) -> float:
    """The greatest distance between any two fixes."""
    return max((_km(a, (b.lat, b.lon)) for a, b in combinations(points, 2)), default=0.0)


def _candidate(mac: str, away: Away, trips: int, spread: float) -> dict[str, Any]:
    latest = away[-1][0]
    return {
        "mac": mac,
        "kind": latest.kind,
        "name": next((s.name for s, _ in reversed(away) if s.name), ""),
        "trips": trips,
        "spread_km": round(spread, 1),
        "last_seen": latest.last.t,
        "seen": [
            {"start": s.first.t, "end": s.last.t, "places": [[round(p.lat, 4), round(p.lon, 4)] for p in ps]}
            for s, ps in away
        ],
    }


class NullFollows:
    """No Kafka: nothing is published, and there are no candidates."""

    available = False

    def __init__(self) -> None:
        self.history = History()

    def publish(self, found: list[Sighting]) -> None:
        pass

    def publish_candidates(self, candidates: list[dict[str, Any]], rules: Rules) -> None:
        pass

    def close(self) -> None:
        pass


class Follows(NullFollows):
    """The sightings topic, read whole into a History, and the producer for both topics."""

    available = True

    def __init__(self, bootstrap: str, timeout: float = READ_TIMEOUT) -> None:
        self.timeout = timeout
        self.history = History()
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": "wigle-sync-follows",
                "enable.auto.commit": False,
                "enable.partition.eof": True,
                "socket.timeout.ms": 15_000,
            },
            logger=KAFKA_LOG,
        )
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "acks": "all",
                "enable.idempotence": True,
                "message.timeout.ms": 15_000,
                "linger.ms": 50,
            },
            logger=KAFKA_LOG,
        )

    def load(self) -> None:
        for key, value in read_topic(self._consumer, SIGHTINGS_TOPIC, self.timeout):
            if value is None:
                self.history.forget(key)
                continue
            try:
                self.history.add(Sighting.from_record(json.loads(value)))
            except (ValueError, KeyError, TypeError):
                log.warning("Unreadable sighting, ignoring it")

    def publish(self, found: list[Sighting]) -> None:
        for s in found:
            self.history.add(s)
            self._producer.produce(SIGHTINGS_TOPIC, key=s.key, value=json.dumps(s.record()))
            self._producer.poll(0)

    def publish_candidates(self, candidates: list[dict[str, Any]], rules: Rules) -> None:
        value = {
            "at": time.time(),
            "rules": {"min_trips": rules.min_trips, "min_km": rules.min_km, "home_km": rules.home_km},
            "devices": len(self.history.devices),
            "sightings": len(self.history),
            "candidates": candidates,
        }
        self._producer.produce(FOLLOWS_TOPIC, key=FOLLOWS_KEY, value=json.dumps(value))
        self._producer.poll(0)

    def abandon(self) -> None:
        self._producer.purge()
        self._consumer.close()

    def close(self) -> None:
        left = self._producer.flush(30)
        if left:
            log.warning("Sightings not delivered", undelivered=left)
        self._consumer.close()


def open_follows(bootstrap: str | None) -> NullFollows:
    """The history, loaded, or a NullFollows when Kafka is unset or unreadable:
    the upload never waits on this."""
    if not bootstrap:
        return NullFollows()
    follows = Follows(bootstrap)
    try:
        follows.load()
    except (KafkaException, TimeoutError) as e:
        log.warning("Sightings unreadable, no follows-me check this run", error=str(e))
        follows.abandon()
        return NullFollows()
    log.info("Sightings loaded", devices=len(follows.history.devices), sightings=len(follows.history))
    return follows


def latest_candidates(bootstrap: str, timeout: float = 15.0) -> dict[str, Any] | None:
    """The newest candidate list on wigle-sync.follows (the console), or None
    when there is none yet. Raises KafkaException or TimeoutError."""
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": "wigle-console-follows",
            "enable.auto.commit": False,
            "enable.partition.eof": True,
        },
        logger=KAFKA_LOG,
    )
    try:
        latest = None
        for key, value in read_topic(consumer, FOLLOWS_TOPIC, timeout):
            if key == FOLLOWS_KEY and value is not None:
                latest = value
        return json.loads(latest) if latest else None
    finally:
        consumer.close()
