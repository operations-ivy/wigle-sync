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
        "12:34",
    ]


def test_at_home_with_kismet_stopped_and_charging():
    lines = eink.screen_lines(
        driving(home=True, kismet_up=False, sources={}, recent=None, charging=True, gps_mode=1, satellites=0)
    )
    assert lines[:4] == ["HOME  GPS no fix", "Kismet stopped", "Heard -", "Battery 78% +"]


def test_unknowns():
    lines = eink.screen_lines(eink.Status(now=NOON))
    assert lines == ["?  GPS ?", "Kismet stopped", "Heard -", "Battery ?", "12:34 ?"]


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


@pytest.mark.parametrize("n, shown", [(0, 0), (19, 19), (22, 20), (23, 25), (99, 100), (104, 100), (1234, 1230)])
def test_rounded(n, shown):
    assert eink.rounded(n) == shown


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
    image = eink.draw(eink.screen_lines(driving()))
    assert image.size == (eink.WIDTH, eink.HEIGHT)
    assert image.mode == "1"
