"""#45: every line wigle-sync, wigle-console and status-leds write is one JSON
object with timestamp, level, event, logger and service."""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from unittest import mock

import pytest
import structlog

import brick_observe

FIELDS = {"timestamp", "level", "event", "logger", "service"}


@pytest.fixture(autouse=True)
def restore_logging():
    """These tests configure logging globally, onto pytest's captured stdout:
    put it back, or later tests log to a closed file."""
    config, handlers = structlog.get_config(), logging.getLogger().handlers[:]
    yield
    structlog.configure(**config)
    logging.getLogger().handlers[:] = handlers


def lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_structlog_and_library_lines_share_one_json_format(capsys):
    with mock.patch.dict("os.environ", {"LOG_FORMAT": "json"}):
        brick_observe.init("wigle-sync", stream=sys.stdout, tracing_enabled=False)
    brick_observe.get_logger(__name__).info("Sync complete", uploaded=2)
    logging.getLogger("werkzeug").warning("a library's own line")
    ours, theirs = lines(capsys.readouterr().out)
    assert FIELDS <= ours.keys() and FIELDS <= theirs.keys()
    assert (ours["service"], ours["logger"], ours["uploaded"]) == ("wigle-sync", __name__, 2)
    assert (theirs["logger"], theirs["level"], theirs["event"]) == ("werkzeug", "warning", "a library's own line")


def test_status_leds_journal_lines_are_json(capsys):
    spec = importlib.util.spec_from_file_location(
        "status_leds", Path(__file__).resolve().parent.parent / "pi" / "leds" / "status-leds.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with mock.patch.dict("os.environ", {"LOG_FORMAT": "json"}):
        log = module.init_logging()
    log.info("heat", level="orange", celsius=66.1, average_celsius=65.2)
    [line] = lines(capsys.readouterr().out)
    assert FIELDS <= line.keys()
    assert (line["service"], line["event"], line["level"], line["celsius"]) == ("status-leds", "heat", "info", 66.1)
