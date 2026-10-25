"""wigle-sync's ledger of uploaded files, in Kafka (#21).

The Pi's uploaded/ directory says what's been sent, but only on the Pi's SD
card: a session copied back, or a wiped uploaded/, would be uploaded again.
The ledger is a compacted topic (wigle-sync.files, brick-k8s-config's
kafka/), one record per .wiglecsv keyed by the SHA-256 of its bytes, so a
renamed or re-copied session is still recognised. Each run reads the whole
topic (one small partition), skips any file already uploaded, and records
each new upload with WiGLE's transaction.

Best effort by design: with Kafka unset or unreachable the sync runs as it
always did, on uploaded/ alone, and says so in its log.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import structlog
from confluent_kafka import Consumer
from confluent_kafka import KafkaError
from confluent_kafka import KafkaException
from confluent_kafka import OFFSET_BEGINNING
from confluent_kafka import Producer
from confluent_kafka import TopicPartition

log = structlog.get_logger()

TOPIC = "wigle-sync.files"
# librdkafka's own messages go through stdlib logging, so they're JSON like
# every other line (observability.init_logging) rather than raw stderr.
KAFKA_LOG = logging.getLogger("kafka")


def file_key(path: Path) -> str:
    """The ledger's key for a file: the SHA-256 of its bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class NullLedger:
    """No ledger: nothing is known to be uploaded, and nothing is recorded."""

    available = False

    def uploaded(self, key: str) -> dict[str, Any] | None:
        return None

    def record(self, key: str, state: str, **fields: Any) -> None:
        pass

    def close(self) -> None:
        pass


class Ledger(NullLedger):
    available = True

    def __init__(self, bootstrap: str, topic: str = TOPIC, timeout: float = 15.0) -> None:
        self.topic, self.timeout = topic, timeout
        self.states: dict[str, dict[str, Any]] = {}
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": "wigle-sync-ledger",
                "enable.auto.commit": False,
                "enable.partition.eof": True,
                "socket.timeout.ms": int(timeout * 1000),
            },
            logger=KAFKA_LOG,
        )
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "acks": "all",
                "enable.idempotence": True,
                "message.timeout.ms": int(timeout * 1000),
            },
            logger=KAFKA_LOG,
        )
        self._failed: list[str] = []

    def load(self) -> None:
        """Read the whole topic into states (newest record per key wins).
        Raises KafkaException or TimeoutError when Kafka can't be read."""
        meta = self._consumer.list_topics(self.topic, timeout=self.timeout).topics.get(self.topic)
        if meta is None or meta.error is not None:
            raise KafkaException(meta.error if meta else KafkaError(KafkaError.UNKNOWN_TOPIC_OR_PART))
        parts = [TopicPartition(self.topic, p, OFFSET_BEGINNING) for p in meta.partitions]
        # Partitions with nothing in them would never report EOF.
        pending = set()
        for tp in parts:
            low, high = self._consumer.get_watermark_offsets(tp, timeout=self.timeout)
            if high > low:
                pending.add(tp.partition)
        self._consumer.assign(parts)
        deadline = time.monotonic() + self.timeout
        while pending:
            if time.monotonic() > deadline:
                raise TimeoutError(f"reading {self.topic}: partitions {sorted(pending)} unfinished")
            msg = self._consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    pending.discard(msg.partition())
                    continue
                raise KafkaException(msg.error())
            key = msg.key().decode() if msg.key() else None
            if key is None:
                continue
            if msg.value() is None:  # a tombstone: forgotten
                self.states.pop(key, None)
            else:
                try:
                    self.states[key] = json.loads(msg.value())
                except ValueError:
                    log.warning("Unreadable ledger record, ignoring it", key=key)

    def uploaded(self, key: str) -> dict[str, Any] | None:
        state = self.states.get(key)
        return state if state and state.get("state") == "uploaded" else None

    def record(self, key: str, state: str, **fields: Any) -> None:
        value = {"state": state, "at": time.time(), **fields}
        self.states[key] = value

        def delivered(err, msg) -> None:
            if err is not None:
                self._failed.append(key)

        self._producer.produce(self.topic, key=key, value=json.dumps(value), on_delivery=delivered)
        self._producer.poll(0)

    def abandon(self) -> None:
        """Close without sending anything (a ledger that never loaded)."""
        self._producer.purge()
        self._consumer.close()

    def close(self) -> None:
        left = self._producer.flush(self.timeout)
        if left or self._failed:
            log.warning("Ledger records not delivered", undelivered=left, failed=len(self._failed))
        self._consumer.close()


def open_ledger(bootstrap: str | None, topic: str = TOPIC) -> NullLedger:
    """The ledger, loaded, or a NullLedger when it's unset or can't be read."""
    if not bootstrap:
        return NullLedger()
    ledger = Ledger(bootstrap, topic)
    try:
        ledger.load()
    except (KafkaException, TimeoutError) as e:
        log.warning("Ledger unreadable, going by the Pi's uploaded/ alone", error=str(e))
        ledger.abandon()
        return NullLedger()
    log.info(
        "Ledger loaded",
        files=len(ledger.states),
        uploaded=sum(1 for s in ledger.states.values() if s.get("state") == "uploaded"),
    )
    return ledger
