"""pi/eink/eink-status.py: the screen's text and when it's redrawn, without a
panel (loaded by path: the script has a dash in its name)."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
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
    sources={"usb_bt": "ok", "wlan_mon0": "ok"},
    recent={"wifi": 247, "bluetooth": 31},
    battery_percent=78.4,
    charging=False,
    last_sync=NOON.timestamp() - 3 * 3600 - 60,
    to_send=2,
    clock_synced=True,
    now=NOON,
)


def driving(**changes):
    return dataclasses.replace(DRIVING, **changes)


def test_on_the_road():
    assert eink.screen_lines(driving()) == [
        "AWAY  GPS 3D 9 sat",
        "BT ok   Wi-Fi ok",
        "Heard 250 Wi-Fi  30 BT",
        "Battery 78%",
        "Sync 3h ago",
        "12:34  2 to send",
    ]


def test_at_home_with_kismet_stopped_and_charging():
    lines = eink.screen_lines(
        driving(home=True, kismet_up=False, sources={}, recent=None, charging=True, gps_mode=1, satellites=0)
    )
    assert lines[:4] == ["HOME  GPS no fix", "Kismet stopped", "Heard -", "Battery 78% +"]


def test_unknowns():
    lines = eink.screen_lines(eink.Status(now=NOON))
    assert lines == ["?  GPS ?", "Kismet stopped", "Heard -", "Battery ?", "Sync never", "12:34 ?"]


def test_a_source_in_error():
    lines = eink.screen_lines(driving(sources={"usb_bt": "off", "wlan_mon0": "ERR"}))
    assert lines[1] == "BT off   Wi-Fi ERR"
    assert eink.screen_lines(driving(sources={}))[1] == "Kismet: no sources"


@pytest.mark.parametrize(
    "mode, sats, text", [(3, 9, "GPS 3D 9 sat"), (2, 4, "GPS 2D 4 sat"), (1, 0, "GPS no fix"), (0, 0, "GPS ?")]
)
def test_gps_text(mode, sats, text):
    assert eink.gps_text(mode, sats) == text


def test_source_state_and_label():
    assert eink.source_state({"kismet.datasource.running": 1, "kismet.datasource.error": 0}) == "ok"
    assert eink.source_state({"kismet.datasource.running": 1, "kismet.datasource.error": 1}) == "ERR"
    assert eink.source_state({"kismet.datasource.running": 0}) == "off"
    assert eink.source_label("usb_bt") == "BT"
    assert eink.source_label("hci0") == "BT"
    assert eink.source_label("wlan_mon0") == "Wi-Fi"


@pytest.mark.parametrize(
    "n, shown",
    [
        (0, "0"),
        (19, "19"),
        (22, "20"),
        (23, "25"),
        (99, "100"),
        (104, "100"),
        (994, "990"),
        (995, "1.0k"),
        (1234, "1.2k"),
    ],
)
def test_rounded(n, shown):
    assert eink.rounded(n) == shown


@pytest.mark.parametrize(
    "seconds, shown", [(-60, "<1h"), (0, "<1h"), (3599, "<1h"), (3600, "1h"), (47 * 3600, "47h"), (48 * 3600, "2d")]
)
def test_ago(seconds, shown):
    assert eink.ago(seconds) == shown


def test_sync_text():
    assert eink.sync_text(None, NOON, True) == "Sync never"
    assert eink.sync_text(NOON.timestamp() - 5 * 3600, NOON, True) == "Sync 5h ago"
    # Without the real time, "ago" means nothing: the sync's own time instead.
    assert eink.sync_text(datetime(2026, 11, 14, 21, 5).timestamp(), NOON, False) == "Sync 11-14 21:05"


def test_nothing_to_send_leaves_the_clock_alone():
    assert eink.screen_lines(driving(to_send=0))[-1] == "12:34"


@pytest.mark.parametrize(
    "changes, face",
    [
        ({}, "stoked"),
        ({"gps_mode": 1}, "searching"),
        ({"home": True, "kismet_up": False, "sources": {}}, "sleepy"),
        ({"home": None, "kismet_up": False, "sources": {}}, "sleepy"),
        ({"home": False, "kismet_up": False, "sources": {}}, "uh oh"),
        ({"sources": {"usb_bt": "ok", "wlan_mon0": "ERR"}}, "uh oh"),
        ({"sources": {}}, "uh oh"),
        ({"battery_percent": 20.0}, "low"),
        ({"battery_percent": 20.0, "charging": True}, "stoked"),
        ({"battery_percent": None}, "stoked"),
    ],
)
def test_mood(changes, face):
    assert eink.mood(driving(**changes)) == face


@pytest.mark.parametrize("face", list(eink.FACES))
def test_sprites_are_16_by_16(face):
    rows = eink.sprite(face)
    assert len(rows) == 16
    assert all(len(row) == 16 and set(row) <= {"#", "."} for row in rows)


def test_each_mood_has_its_own_face():
    assert len({tuple(eink.sprite(face)) for face in eink.FACES}) == len(eink.FACES)


def test_last_sync_and_to_send(tmp_path):
    uploaded = tmp_path / "uploaded"
    assert eink.last_sync(uploaded) is None
    uploaded.mkdir()
    assert eink.last_sync(uploaded) is None
    (uploaded / "Kismet-20261116-10-00-00-1.wiglecsv").write_text("x")
    assert eink.last_sync(uploaded) == pytest.approx((uploaded / "Kismet-20261116-10-00-00-1.wiglecsv").stat().st_ctime)
    assert eink.to_send(tmp_path) == 0
    (tmp_path / "Kismet-20261116-12-00-00-1.wiglecsv").write_text("x")
    (tmp_path / "Kismet-20261116-12-00-00-1.kismet").write_text("x")
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
    assert eink.off_lines("battery 9%", True, NOON, True)[:3] == ["OFF", "battery 9%", "2026-11-16 12:34"]
    assert eink.off_lines(None, True, NOON, False)[:3] == ["OFF", "shut down", "2026-11-16 12:34 ?"]
    assert eink.off_lines(None, False, NOON, True)[0] == "DISPLAY STOPPED"


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


def test_draw_fits_the_panel():
    pytest.importorskip("PIL")
    if not Path(eink.FONT).exists():
        pytest.skip("no DejaVu fonts")
    image = eink.draw(eink.screen_lines(driving()), face="stoked")
    assert image.size == (eink.WIDTH, eink.HEIGHT)
    assert image.mode == "1"


@pytest.mark.parametrize(
    "status",
    [
        driving(recent={"wifi": 1234, "bluetooth": 150}, to_send=12, satellites=12),
        driving(sources={"usb_bt": "off", "wlan_mon0": "ERR"}, clock_synced=False, to_send=12),
    ],
)
def test_text_clears_the_mascot(status):
    """The widest lines end left of the mascot (the top line runs over it)."""
    image_font = pytest.importorskip("PIL.ImageFont")
    if not Path(eink.FONT).exists():
        pytest.skip("no DejaVu fonts")
    body, small = image_font.truetype(eink.FONT, 14), image_font.truetype(eink.FONT, 13)
    lines = eink.screen_lines(status)
    mascot_left = eink.WIDTH - 16 * eink.SCALE - 2
    for line in lines[1:-1]:
        assert 4 + body.getlength(line) < mascot_left, line
    assert 4 + small.getlength(lines[-1]) < mascot_left
