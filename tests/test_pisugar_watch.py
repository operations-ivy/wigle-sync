"""pi/pisugar/pisugar-watch.py: pisugar-server's answers, the low-battery
shutdown and the RTC decisions (loaded by path: the script has a dash in its
name)."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "pisugar_watch", Path(__file__).resolve().parent.parent / "pi" / "pisugar" / "pisugar-watch.py"
)
pw = importlib.util.module_from_spec(SPEC)
sys.modules["pisugar_watch"] = pw  # dataclasses look their module up
SPEC.loader.exec_module(pw)

T0 = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def test_parse_reply():
    assert pw.parse_reply("battery: 85.3\n") == ("battery", "85.3")
    assert pw.parse_reply("rtc_time: 2026-11-16T12:00:00.000+00:00") == ("rtc_time", "2026-11-16T12:00:00.000+00:00")
    assert pw.parse_reply("Invalid request") is None
    assert pw.parse_reply("error: no such thing: x") == ("error", "no such thing: x")
    assert pw.parse_reply("") is None


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
    text = pw.render_metrics(pw.Reading(77.5, 3.95, True), 10, True)
    assert "pi_pisugar_up 1\n" in text
    assert "pi_battery_percent 77.5\n" in text
    assert "pi_battery_volts 3.95\n" in text
    assert "pi_battery_charging 1\n" in text
    assert "pi_battery_shutdown_percent 10\n" in text
    assert "# TYPE pi_battery_percent gauge\n" in text


def test_metrics_leave_out_what_is_unknown():
    text = pw.render_metrics(pw.Reading(None, None, None), 10, False)
    assert "pi_pisugar_up 0\n" in text
    assert "pi_battery_percent" not in text
    assert "pi_battery_charging" not in text


def test_rtc_ahead_sets_the_clock_at_boot():
    rtc = T0 + timedelta(days=3)  # off for three days; the clock is at the saved time
    assert pw.rtc_action(T0, rtc, T0, False, None, 3600) == "rtc2pi"


def test_rtc_that_lost_power_is_ignored():
    assert pw.rtc_action(T0, datetime(2000, 1, 1, tzinfo=timezone.utc), T0, False, None, 3600) is None


def test_rtc_behind_the_clock_is_ignored_until_synced():
    assert pw.rtc_action(T0, T0 - timedelta(hours=1), None, False, None, 3600) is None
    assert pw.rtc_action(T0, T0 + timedelta(seconds=1), None, False, None, 3600) is None
    assert pw.rtc_action(T0, None, None, False, None, 3600) is None


def test_synced_clock_writes_the_rtc_when_off_or_due():
    assert pw.rtc_action(T0, T0 + timedelta(seconds=30), None, True, 10, 3600) == "pi2rtc"
    assert pw.rtc_action(T0, None, None, True, 10, 3600) == "pi2rtc"
    assert pw.rtc_action(T0, T0, None, True, None, 3600) == "pi2rtc"
    assert pw.rtc_action(T0, T0, None, True, 3600, 3600) == "pi2rtc"
    assert pw.rtc_action(T0, T0 + timedelta(seconds=1), None, True, 60, 3600) is None


def test_parse_rtc():
    assert pw.parse_rtc("2026-11-16T12:00:00.000+00:00") == T0
    assert pw.parse_rtc("2026-11-16T12:00:00") is None  # no zone: not trusted
    assert pw.parse_rtc("I2C error") is None
    assert pw.parse_rtc(None) is None


def test_fake_hwclock_floor(tmp_path):
    saved = tmp_path / "fake-hwclock.data"
    saved.write_text("2026-11-16 12:00:00\n")
    assert pw.fake_hwclock_floor(saved) == T0
    assert pw.fake_hwclock_floor(tmp_path / "missing") is None


def test_chrony_synced():
    synced = "CB007105,203.0.113.5,3,1763294400.0,0.0001,0.0,0.0,1.0,0.0,0.0,0.01,0.001,64.0,Normal\n"
    assert pw.parse_chrony_synced(synced)
    assert not pw.parse_chrony_synced("00000000,,0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,Not synchronised\n")
    assert not pw.parse_chrony_synced("")


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
    if os.path.exists(path):
        os.unlink(path)


def test_reading_over_the_socket(fake_server):
    reading, up = pw.PiSugar(fake_server).reading()
    assert up
    assert reading == pw.Reading(64.2, 3.88, False)


def test_no_server(tmp_path):
    reading, up = pw.PiSugar(str(tmp_path / "none.sock"), timeout=0.5).reading()
    assert not up
    assert reading == pw.Reading(None, None, None)
