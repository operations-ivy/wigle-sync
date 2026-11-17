"""pi/pisugar/pisugar-watch.py: pisugar-server's answers, the low-battery
shutdown and the RTC decisions (loaded by path: the script has a dash in its
name)."""

from __future__ import annotations

import socket
import threading
from datetime import datetime
from datetime import timedelta
from datetime import UTC

import pytest

from tests.conftest import load_script
from tests.conftest import PI

pw = load_script("pisugar_watch", PI / "pisugar" / "pisugar-watch.py")

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "line, parsed",
    [
        ("battery: 85.3\n", ("battery", "85.3")),
        ("rtc_time: 2026-11-16T12:00:00.000+00:00", ("rtc_time", "2026-11-16T12:00:00.000+00:00")),
        ("Invalid request", None),
        ("error: no such thing: x", ("error", "no such thing: x")),
        ("", None),
    ],
)
def test_parse_reply(line, parsed):
    assert pw.parse_reply(line) == parsed


def test_reading_from_pisugar_2_answers():
    r = pw.reading_from({"battery": "85.3", "battery_v": "4.01", "battery_charging": "false"})
    assert r == pw.Reading(85.3, 4.01, False)
    assert r.ok


def test_reading_errors_and_impossible_values_are_unknown():
    r = pw.reading_from({"battery": "I2C bus error", "battery_v": "0.0", "battery_charging": "maybe"})
    assert r == pw.Reading(None, None, None)
    assert not r.ok
    assert pw.reading_from({"battery": "101"}).percent is None


def test_either_power_flag_means_charging():
    assert pw.reading_from({"battery_charging": "false", "battery_power_plugged": "true"}).charging is True
    assert pw.reading_from({"battery_power_plugged": "false"}).charging is False


def low(percent=8.0, volts=3.6, charging=False):
    return pw.Reading(percent, volts, charging)


def test_shutdown_needs_readings_in_a_row():
    guard = pw.ShutdownGuard(10, 3.5, 3)
    assert [guard.update(low()) for _ in range(3)] == [False, False, True]


def test_one_good_reading_starts_the_count_again():
    guard = pw.ShutdownGuard(10, 3.5, 3)
    guard.update(low())
    guard.update(low())
    assert not guard.update(pw.Reading(40.0, 3.9, False))
    assert not guard.update(low())
    assert guard.low == 1


def test_charging_never_shuts_down():
    guard = pw.ShutdownGuard(10, 3.5, 2)
    assert not any(guard.update(low(percent=2.0, charging=True)) for _ in range(5))


def test_voltage_alone_counts():
    guard = pw.ShutdownGuard(10, 3.5, 2)
    sag = pw.Reading(30.0, 3.45, False)
    assert [guard.update(sag), guard.update(sag)] == [False, True]


def test_no_answer_neither_counts_nor_resets():
    guard = pw.ShutdownGuard(10, 3.5, 2)
    guard.update(low())
    assert not guard.update(pw.Reading(None, None, None))
    assert guard.update(low())


def test_metrics():
    text = pw.render_metrics(pw.Reading(77.5, 3.95, True), 10)
    assert "pi_pisugar_up 1\n" in text
    assert "pi_battery_percent 77.5\n" in text
    assert "pi_battery_volts 3.95\n" in text
    assert "pi_battery_charging 1\n" in text
    assert "pi_battery_shutdown_percent 10\n" in text
    assert "# TYPE pi_battery_percent gauge\n" in text


def test_metrics_leave_out_what_is_unknown():
    text = pw.render_metrics(pw.Reading(None, None, None), 10)
    assert "pi_pisugar_up 0\n" in text
    assert "pi_battery_percent" not in text
    assert "pi_battery_charging" not in text


def test_rtc_ahead_sets_the_clock_at_boot():
    rtc = T0 + timedelta(days=3)  # off for three days; the clock is at the saved time
    assert pw.rtc_action(T0, rtc, T0, False, None, 3600) == "rtc2pi"


def test_rtc_that_lost_power_is_ignored():
    assert pw.rtc_action(T0, datetime(2000, 1, 1, tzinfo=UTC), T0, False, None, 3600) is None


@pytest.mark.parametrize(
    "rtc",
    [T0 - timedelta(hours=1), T0 + timedelta(seconds=1), None],
    ids=["behind", "within tolerance", "unreadable"],
)
def test_rtc_left_alone_until_synced(rtc):
    assert pw.rtc_action(T0, rtc, None, False, None, 3600) is None


@pytest.mark.parametrize(
    "rtc, since_write, action",
    [
        (T0 + timedelta(seconds=30), 10, "pi2rtc"),
        (None, 10, "pi2rtc"),
        (T0, None, "pi2rtc"),
        (T0, 3600, "pi2rtc"),
        (T0 + timedelta(seconds=1), 60, None),
    ],
    ids=["off", "unreadable", "never written", "due", "close and recent"],
)
def test_synced_clock_writes_the_rtc_when_off_or_due(rtc, since_write, action):
    assert pw.rtc_action(T0, rtc, None, True, since_write, 3600) == action


@pytest.mark.parametrize(
    "value, parsed",
    [
        ("2026-11-16T12:00:00.000+00:00", T0),
        ("2026-11-16T12:00:00", None),  # no zone: not trusted
        ("I2C error", None),
        (None, None),
    ],
)
def test_parse_rtc(value, parsed):
    assert pw.parse_rtc(value) == parsed


def test_fake_hwclock_floor(tmp_path):
    saved = tmp_path / "fake-hwclock.data"
    saved.write_text("2026-11-16 12:00:00\n")
    assert pw.fake_hwclock_floor(saved) == T0
    assert pw.fake_hwclock_floor(tmp_path / "missing") is None


def test_config_defaults_and_overrides(tmp_path):
    conf = tmp_path / "handheld.yaml"
    conf.write_text("battery:\n  shutdown_percent: 15\n  model: PiSugar 2 (2-LEDs)\n  unknown: 1\n")
    config = pw.load_config(conf)
    assert config["shutdown_percent"] == 15
    assert config["readings"] == pw.DEFAULTS["readings"]
    assert "unknown" not in config and "model" not in config
    assert pw.load_config(tmp_path / "missing") == pw.DEFAULTS


@pytest.fixture
def fake_server(tmp_path):
    """A stand-in pisugar-server on a Unix socket: one command, one answer."""
    path = str(tmp_path / "pisugar.sock")
    answers = {
        "get battery": "battery: 64.2",
        "get battery_v": "battery_v: 3.88",
        "get battery_charging": "battery_charging: false",
        "get battery_power_plugged": "Invalid request",
    }
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen()

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                command = conn.makefile().readline().strip()
                conn.sendall((answers.get(command, "Invalid request") + "\n").encode())

    threading.Thread(target=serve, daemon=True).start()
    yield path
    srv.close()


def test_reading_over_the_socket(fake_server):
    reading, answered = pw.PiSugar(fake_server).reading()
    assert answered
    assert reading == pw.Reading(64.2, 3.88, False)


def test_no_server(tmp_path):
    reading, answered = pw.PiSugar(str(tmp_path / "none.sock"), timeout=0.5).reading()
    assert not answered
    assert reading == pw.Reading(None, None, None)


@pytest.mark.parametrize(
    ("reading", "answered", "expected"),
    [
        (pw.Reading(77.5, 3.95, False), True, "battery readings"),
        # pisugar-server answers "I2C not connected" when the board's pins don't touch.
        (pw.Reading(None, None, None), True, "the board isn't on I2C"),
        (pw.Reading(None, None, None), False, "pisugar-server not answering"),
    ],
)
def test_board_state_tells_a_silent_board_from_a_silent_server(reading, answered, expected):
    assert expected in pw.board_state(reading, answered)


def test_server_answering_without_the_board_is_not_up():
    replies = {
        name: "I2C not connected" for name in ("battery", "battery_v", "battery_charging", "battery_power_plugged")
    }
    reading = pw.reading_from(replies)
    assert "pi_pisugar_up 0\n" in pw.render_metrics(reading, 10)
