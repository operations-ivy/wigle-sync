"""What the handheld's services share (eink-status, pisugar-watch): chrony's
sync state, structlog's setup, atomic writes and SIGTERM as an Event.
setup-eink.sh and setup-pisugar.sh install it beside each script, which
imports it from its own directory."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

import structlog

# chronyc's reference IDs when it has no real source: none, or its local clock.
UNSYNCED_REFS = ("", "00000000", "7F7F0101")


def parse_chrony_synced(csv_text: str) -> bool:
    """`chronyc -c tracking`: the reference ID is the first field, the leap
    status the last. Unsynchronised, chrony reports reference 0 or
    "Not synchronised"."""
    fields = csv_text.strip().split(",")
    if len(fields) < 2:
        return False
    return fields[0] not in UNSYNCED_REFS and fields[-1] != "Not synchronised"


def chrony_synced() -> bool:
    """Whether chrony has the real time (from the internet or the GPS)."""
    try:
        r = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and parse_chrony_synced(r.stdout)


def init_logging(service: str) -> structlog.typing.FilteringBoundLogger:
    """JSON lines when the unit sets LOG_FORMAT=json, as status-leds does, so
    the journal's events are fields once host journals reach Loki."""
    json_out = os.environ.get("LOG_FORMAT") == "json"
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            lambda _, __, event_dict: {**event_dict, "logger": service, "service": service},
            structlog.processors.JSONRenderer() if json_out else structlog.dev.ConsoleRenderer(colors=False),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    return structlog.get_logger()


def write_atomic(path: Path, text: str) -> None:
    """Write text to path through a temp file beside it, so a reader never
    sees half a file; the temp file goes if anything fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def stop_on_sigterm() -> threading.Event:
    """An Event set by SIGTERM: loops wait on it instead of sleeping."""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return stop
