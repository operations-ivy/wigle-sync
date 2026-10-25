"""pi/leds/status-leds.py's matching and heat levels (loaded by path: the script
has a dash in its name and imports detect from beside it)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

LEDS = Path(__file__).resolve().parent.parent / "pi" / "leds"
sys.path.insert(0, str(LEDS))
SPEC = importlib.util.spec_from_file_location("status_leds", LEDS / "status-leds.py")
leds = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(leds)


def device(mac, name, phy="IEEE802.11", type_="Wi-Fi AP"):
    return {
        "kismet.device.base.key": f"{phy}_{mac}",
        "kismet.device.base.type": type_,
        "kismet.device.base.macaddr": mac,
        "kismet.device.base.commonname": name,
        "kismet.device.base.phyname": phy,
    }


def test_flock_ssid_alerts():
    assert leds.kinds_near([device("B4:1E:52:00:00:01", "Flock-1A2B3C")]) == {"flock"}


def test_flock_name_on_bluetooth_does_not():
    assert leds.kinds_near([device("B4:1E:52:00:00:01", "Flock-1A2B3C", "Bluetooth")]) == set()


def test_axon_oui_alerts_on_either_phy():
    assert leds.kinds_near([device("00:25:DF:12:34:56", "", "Bluetooth")]) == {"axon"}
    assert leds.kinds_near([device("00:25:df:12:34:56", "x")]) == {"axon"}


def test_flipper_and_ordinary_devices_do_not():
    assert (
        leds.kinds_near(
            [device("AA:BB:CC:00:00:01", "Flipper Bob", "Bluetooth"), device("AA:BB:CC:00:00:02", "HomeNet")]
        )
        == set()
    )


def test_both_kinds():
    assert leds.kinds_near([device("00:25:DF:12:34:56", ""), device("B4:1E:52:00:00:01", "Flock-ABCD")]) == {
        "axon",
        "flock",
    }


def test_credentials(tmp_path):
    conf = tmp_path / "kismet_httpd.conf"
    conf.write_text("httpd_username=wigle\nhttpd_password=abc=def\n")
    assert leds.credentials(conf) == ("wigle", "abc=def")


def test_pwm_chip_found_by_pin(tmp_path):
    for n, pin in enumerate((17, 27)):
        dev = tmp_path / "devices" / f"pwm_gpio@{pin:x}"
        dev.mkdir(parents=True)
        (tmp_path / f"pwmchip{n}").mkdir()
        (tmp_path / f"pwmchip{n}" / "device").symlink_to(dev)
    assert leds.pwm_chip(17, tmp_path) == tmp_path / "pwmchip0"
    assert leds.pwm_chip(27, tmp_path) == tmp_path / "pwmchip1"


def test_pwm_chip_missing(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        leds.pwm_chip(17, tmp_path)


def test_fader_writes_duty_only_on_change(tmp_path):
    pwm = tmp_path / "pwm0"
    pwm.mkdir()
    for f in ("duty_cycle", "period", "enable"):
        (pwm / f).write_text("")
    green = leds.Fader(tmp_path)
    assert (pwm / "period").read_text() == str(leds.PERIOD)
    assert (pwm / "enable").read_text() == "1"
    green.set(0.5)
    assert (pwm / "duty_cycle").read_text() == str(leds.PERIOD // 2)
    (pwm / "duty_cycle").write_text("untouched")
    green.set(0.5)
    assert (pwm / "duty_cycle").read_text() == "untouched"
    green.set(2)
    assert (pwm / "duty_cycle").read_text() == str(leds.PERIOD)


def test_heat_levels():
    assert leds.heat_level(40, 40) == "green"
    assert leds.heat_level(56, 55) == "yellow"
    assert leds.heat_level(66, 65) == "orange"
    assert leds.heat_level(75, 60) == "red"


def test_orange_holds_through_a_brief_dip():
    # The reading drops to normal, but the average is still hot.
    assert leds.heat_level(50, 66, "orange") == "orange"


def test_a_level_clears_only_under_its_hysteresis():
    assert leds.heat_level(64, 64, "orange") == "orange"
    assert leds.heat_level(62.9, 62.9, "orange") == "yellow"
    assert leds.heat_level(73.5, 60, "red") == "red"
    assert leds.heat_level(72.9, 60, "red") == "yellow"
    assert leds.heat_level(54, 54, "yellow") == "yellow"
    assert leds.heat_level(54, 54, "green") == "green"


def test_rising_uses_the_threshold_itself():
    assert leds.heat_level(64, 64, "yellow") == "yellow"
    assert leds.heat_level(74, 50, "green") == "green"


def test_read_temperature(tmp_path):
    zone = tmp_path / "temp"
    zone.write_text("39704\n")
    assert leds.read_temperature(zone) == 39.704
    zone.write_text("")
    assert leds.read_temperature(zone) is None
    assert leds.read_temperature(tmp_path / "missing") is None


def test_every_level_has_a_colour():
    assert set(leds.COLOURS) == set(leds.LEVELS)


def test_demo_shows_every_level_then_the_alert():
    states = leds.demo_states()
    assert [level for _, _, level in states[:-1]] == list(leds.LEVELS)
    assert not any(near for _, near, _ in states[:-1])
    assert states[-1] == ("LED2 alert", True, None)
