"""pi/eink/eink-status.py: the screen's text and when it's redrawn, without a
panel (loaded by path: the script has a dash in its name)."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest

from tests.conftest import load_script
from tests.conftest import PI

eink = load_script("eink_status", PI / "eink" / "eink-status.py")

NOON = datetime(2026, 11, 16, 12, 34)


DRIVING = eink.Status(
    home=False,
    gps_mode=3,
    satellites=9,
    kismet_up=True,
    sources={"usb_bt": "ok", "wlan_mon0": "ok", "wlan_mon1": "ok"},
    recent={"wifi": 247, "bluetooth": 31},
    battery_percent=78.4,
    charging=False,
    to_send=2,
    clock_synced=True,
    now=NOON,
)


def driving(**changes):
    return dataclasses.replace(DRIVING, **changes)


def test_on_the_road():
    assert eink.screen_lines(driving()) == ["12:34", "GPS 3D 9 SAT", "NO ERRORS"]


def test_waiting_for_a_fix_without_the_real_time():
    assert eink.screen_lines(driving(gps_mode=1, satellites=0, clock_synced=False)) == [
        "12:34?",
        "GPS NO FIX",
        "NO ERRORS",
    ]


def test_kismet_stopped_is_an_error_only_away_from_home():
    stopped = {"kismet_up": False, "sources": {}, "recent": None}
    assert eink.screen_lines(driving(home=True, **stopped))[2:] == ["NO ERRORS"]
    assert eink.screen_lines(driving(home=False, **stopped))[2:] == ["KISMET DOWN"]
    assert eink.screen_lines(driving(home=None, **stopped))[2:] == ["KISMET DOWN"]


def test_unknowns():
    assert eink.screen_lines(eink.Status(now=NOON)) == ["12:34?", "GPS ?", "KISMET DOWN"]


@pytest.mark.parametrize(
    "changes, shown",
    [
        ({"sources": {}}, ["NO SOURCES"]),
        ({"battery_percent": 20.0}, ["BATT 20%"]),
        ({"battery_percent": 20.0, "charging": True}, ["NO ERRORS"]),
        ({"battery_percent": None, "charging": None}, ["NO ERRORS"]),
        (
            {"battery_percent": 15.0, "sources": {"usb_bt": "ERR", "wlan_mon0": "off", "wlan_mon1": "ERR"}},
            ["BATT 15% BT ERR", "5G OFF 2G ERR"],
        ),
    ],
)
def test_errors_wrap_onto_a_second_line(changes, shown):
    assert eink.screen_lines(driving(**changes))[2:] == shown


@pytest.mark.parametrize(
    "sources, shown",
    [
        ({"usb_bt": "ok", "wlan_mon0": "ok", "wlan_mon1": "ok"}, []),
        ({"usb_bt": "ok", "wlan_mon0": "ok", "wlan_mon1": "ERR"}, ["2G ERR"]),
        ({"usb_bt": "ok", "wlan_mon0": "off", "wlan_mon1": "ok"}, ["5G OFF"]),
        ({"usb_bt": "off", "wlan_mon0": "ERR", "wlan_mon1": "off"}, ["BT OFF", "5G ERR", "2G OFF"]),
        ({"usb_bt": "ok", "wlan_mon0": "ERR"}, ["WIFI ERR"]),
        ({"wlan_mon0": "ok", "wlan_mon2": "off"}, ["wlan_mon2 OFF"]),
    ],
)
def test_source_errors_name_wifi_radios_by_band(sources, shown):
    assert eink.source_errors(sources) == shown


@pytest.mark.parametrize(
    "words, lines",
    [
        ([], []),
        (["KISMET DOWN"], ["KISMET DOWN"]),
        (["BATT 15%", "BT ERR", "5G OFF"], ["BATT 15% BT ERR", "5G OFF"]),
    ],
)
def test_wrapped(words, lines):
    assert eink.wrapped(words) == lines


@pytest.mark.parametrize(
    "mode, sats, text", [(3, 9, "GPS 3D 9 SAT"), (2, 4, "GPS 2D 4 SAT"), (1, 0, "GPS NO FIX"), (0, 0, "GPS ?")]
)
def test_gps_text(mode, sats, text):
    assert eink.gps_text(mode, sats) == text


def test_source_state_and_label():
    assert eink.source_state({"kismet.datasource.running": 1, "kismet.datasource.error": 0}) == "ok"
    assert eink.source_state({"kismet.datasource.running": 1, "kismet.datasource.error": 1}) == "ERR"
    assert eink.source_state({"kismet.datasource.running": 0}) == "off"
    assert eink.source_label("usb_bt") == "bt"
    assert eink.source_label("hci0") == "bt"
    assert eink.source_label("wlan_mon0") == "wifi"


@pytest.mark.parametrize(
    "changes, face",
    [
        ({}, "cool"),
        ({"recent": {"wifi": 350, "bluetooth": 60}}, "excited"),
        ({"gps_mode": 1}, "look_l"),  # 12:34: the first five minutes of the turn
        ({"gps_mode": 1, "now": NOON + timedelta(minutes=5)}, "look_r"),
        ({"home": True, "kismet_up": False, "sources": {}, "to_send": 0}, "sleep"),
        (
            {"home": True, "kismet_up": False, "sources": {}, "to_send": 0, "now": NOON + timedelta(minutes=11)},
            "sleep2",
        ),
        ({"home": None, "kismet_up": False, "sources": {}}, "sleep"),
        ({"home": True, "kismet_up": False, "sources": {}}, "upload2"),  # 12:34: the third step
        ({"home": True, "kismet_up": False, "sources": {}, "now": NOON + timedelta(minutes=2)}, "upload"),
        ({"home": False, "kismet_up": False, "sources": {}}, "sad"),
        ({"sources": {"usb_bt": "ok", "wlan_mon0": "ERR"}}, "broken"),
        ({"sources": {}}, "broken"),
        ({"battery_percent": 20.0}, "demotivated"),
        ({"battery_percent": 20.0, "charging": True}, "cool"),
        ({"battery_percent": None}, "cool"),
    ],
)
def test_emotion(changes, face):
    assert eink.emotion(driving(**changes)) == face


def test_to_send(tmp_path):
    assert eink.to_send(tmp_path) == 0
    (tmp_path / "Kismet-20261116-12-00-00-1.wiglecsv").write_text("x")
    (tmp_path / "Kismet-20261116-12-00-00-1.kismet").write_text("x")
    (tmp_path / "uploaded").mkdir()
    (tmp_path / "uploaded" / "Kismet-20261116-10-00-00-1.wiglecsv").write_text("x")
    assert eink.to_send(tmp_path) == 1


def test_count_recent():
    devices = [
        {"kismet.device.base.phyname": "IEEE802.11"},
        {"kismet.device.base.phyname": "BTLE"},
        {"kismet.device.base.phyname": "Bluetooth"},
        {"kismet.device.base.phyname": "RTL433"},
    ]
    assert eink.count_recent(devices) == {"wifi": 1, "bluetooth": 2}


def test_off_screens():
    assert eink.off_lines("battery 9%", True, NOON, True) == ["OFF", "BATTERY 9%", "2026-11-16 12:34"]
    assert eink.off_lines(None, True, NOON, False) == ["OFF", "SHUT DOWN", "2026-11-16 12:34?"]
    assert eink.off_lines(None, False, NOON, True)[:2] == ["STOPPED", "DISPLAY STOPPED"]


def test_battery_state_file(tmp_path):
    state = tmp_path / "battery.json"
    assert eink.battery(state, 1000.0) == (None, None)
    state.write_text(json.dumps({"percent": 55.0, "volts": 3.8, "charging": True, "at": 1000.0}))
    assert eink.battery(state, 1100.0) == (55.0, True)
    assert eink.battery(state, 1000.0 + eink.BATTERY_STALE + 1) == (None, None)
    state.write_text("{not json")
    assert eink.battery(state, 1000.0) == (None, None)


@pytest.fixture
def policy():
    return eink.RefreshPolicy(min_interval=30, full_every=3, full_interval=1800)


@pytest.fixture
def shown(policy):
    """A policy that has drawn ["a"] with a full refresh at time 0."""
    policy.done(["a"], "full", 0)
    return policy


def test_first_draw_is_full(policy):
    assert policy.decide(["a"], 0) == "full"


def test_unchanged_is_left_alone(shown):
    assert shown.decide(["a"], 100) is None


def test_changes_wait_for_the_min_interval(shown):
    assert shown.decide(["b"], 10) is None
    assert shown.decide(["b"], 30) == "partial"


def test_full_after_full_every_partials(shown):
    t = 0
    for i in range(3):
        t += 30
        assert shown.decide([str(i)], t) == "partial"
        shown.done([str(i)], "partial", t)
    assert shown.decide(["x"], t + 30) == "full"
    shown.done(["x"], "full", t + 30)
    assert shown.partials == 0


def test_full_after_full_interval(shown):
    assert shown.decide(["b"], 1800) == "full"


def test_config(tmp_path):
    conf = tmp_path / "handheld.yaml"
    conf.write_text("display:\n  model: epd2in13_V3\n  min_interval: 60\n")
    config = eink.load_config(conf)
    assert config["model"] == "epd2in13_V3"
    assert config["min_interval"] == 60
    assert config["full_every"] == eink.DEFAULTS["full_every"]
    assert eink.load_config(tmp_path / "missing") == eink.DEFAULTS


def test_config_rejects_an_unknown_panel(tmp_path):
    conf = tmp_path / "handheld.yaml"
    conf.write_text("display:\n  model: epd7in5\n")
    with pytest.raises(ValueError, match="epd7in5"):
        eink.load_config(conf)


def test_example_config_loads():
    example = Path(__file__).resolve().parent.parent / "pi" / "handheld.example.yaml"
    assert eink.load_config(example)["model"] == "epd2in13_V4"


def test_credentials(tmp_path):
    conf = tmp_path / "kismet_httpd.conf"
    conf.write_text("httpd_username=wigle\nhttpd_password=a=b\n")
    assert eink.credentials(conf) == ("wigle", "a=b")
    assert eink.credentials(tmp_path / "missing") is None


@pytest.fixture
def fonts():
    """DotGothic16 at the time's size and the other lines', or a skip where
    it or Terminus (the face's label) isn't installed."""
    image_font = pytest.importorskip("PIL.ImageFont")
    for path, package in ((eink.FONT, "fonts-dotgothic16"), (eink.LABEL_FONT, "fonts-terminus-otb")):
        if not Path(path).exists():
            pytest.skip(f"no {path} ({package})")
    return image_font.truetype(eink.FONT, eink.BIG), image_font.truetype(eink.FONT, eink.TEXT)


def test_draw_fits_the_panel(fonts):
    image = eink.draw(eink.screen_lines(driving()), face="cool")
    assert image.size == (eink.WIDTH, eink.HEIGHT)
    assert image.mode == "1"


def assert_clear_of_the_face(lines, fonts):
    """Every line ends left of the face's divider, the first at the larger size."""
    big, text = fonts
    left = eink.BORDER + 1 + 3
    divider = eink.WIDTH - 2 - eink.BORDER - eink.FACE_COLUMN
    for i, line in enumerate(lines):
        assert left + (big if i == 0 else text).getlength(line) < divider, line


@pytest.mark.parametrize(
    "status",
    [
        driving(satellites=12, clock_synced=False),
        driving(
            battery_percent=5.0,
            sources={"usb_bt": "ERR", "wlan_mon0": "ERR", "wlan_mon1": "ERR"},
            clock_synced=False,
        ),
        driving(home=False, kismet_up=False, sources={}, battery_percent=5.0),
    ],
)
def test_text_clears_the_face(fonts, status):
    assert_clear_of_the_face(eink.screen_lines(status), fonts)


# Every screen has the mascot (the user, 2026-11-17), errors included.


class FakeLog:
    def __init__(self):
        self.errors = []

    def exception(self, event, **kw):
        self.errors.append(event)


def test_a_collect_error_shows_an_error_screen_with_the_mascot():
    def broken():
        raise KeyError("kismet.device.base.phyname")

    log = FakeLog()
    lines, face = eink.status_screen(broken, log)
    assert lines[:2] == ["ERROR", "KeyError"]
    assert face == "debug"
    assert log.errors == ["collect failed"]


def test_a_good_poll_shows_the_status_and_its_mood():
    lines, face = eink.status_screen(driving, FakeLog())
    assert lines == eink.screen_lines(driving())
    assert face == eink.emotion(driving())


@pytest.mark.parametrize(
    ("reason", "crashed", "face"),
    [
        (None, False, "sleep"),
        ("battery 9%", False, "demotivated"),
        (None, True, "broken"),
        ("battery 9%", True, "broken"),
    ],
)
def test_the_last_screen_keeps_a_mascot_that_fits_why(reason, crashed, face):
    assert eink.off_mood(reason, crashed) == face


def test_draw_needs_a_face():
    with pytest.raises(TypeError):
        eink.draw(["OFF"])  # pylint: disable=no-value-for-parameter


@pytest.mark.parametrize(
    "lines",
    [
        eink.error_lines(KeyError("x"), NOON, False),
        eink.error_lines(ConnectionResetError(), NOON, True),
        eink.off_lines("battery 9%", True, NOON, False),
        eink.off_lines(None, False, NOON, False),
    ],
)
def test_error_and_last_screens_clear_the_face(fonts, lines):
    assert_clear_of_the_face(lines, fonts)
