"""pi/leds/status-leds.py's matching, new-device blinks and breathing (loaded by path: the script
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
    assert leds.kinds_near([device("AA:BB:CC:00:00:01", "Flipper Bob", "Bluetooth"), device("AA:BB:CC:00:00:02", "HomeNet")]) == set()


def test_both_kinds():
    assert leds.kinds_near([device("00:25:DF:12:34:56", ""), device("B4:1E:52:00:00:01", "Flock-ABCD")]) == {"axon", "flock"}


def test_credentials(tmp_path):
    conf = tmp_path / "kismet_httpd.conf"
    conf.write_text("httpd_username=wigle\nhttpd_password=abc=def\n")
    assert leds.credentials(conf) == ("wigle", "abc=def")


def test_breath_rises_and_falls():
    b = leds.BREATH
    assert leds.breath(0) == 0
    assert leds.breath(b / 2) == 1
    assert 0 < leds.breath(b / 4) < 1
    assert abs(leds.breath(b / 4) - leds.breath(3 * b / 4)) < 1e-9
    assert leds.breath(b) == 0


def test_pwm_chip_found_by_pin(tmp_path):
    for n, pin in enumerate((6, 17, 22)):
        dev = tmp_path / "devices" / f"pwm_gpio@{pin:x}"
        dev.mkdir(parents=True)
        (tmp_path / f"pwmchip{n}").mkdir()
        (tmp_path / f"pwmchip{n}" / "device").symlink_to(dev)
    assert leds.pwm_chip(17, tmp_path) == tmp_path / "pwmchip1"
    assert leds.pwm_chip(22, tmp_path) == tmp_path / "pwmchip2"


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


def test_new_devices_counts_aps_and_bluetooth_once():
    seen = set()
    first = [device("AA:00:00:00:00:01", "Net"), device("AA:00:00:00:00:02", "", type_="Wi-Fi Client"), device("AA:00:00:00:00:03", "", "Bluetooth", "BTLE")]
    assert leds.new_devices(first, seen) == (1, 1)
    assert leds.new_devices(first + [device("AA:00:00:00:00:04", "Other")], seen) == (1, 0)
    assert leds.new_devices(first, seen) == (0, 0)


def test_blink_plan_alternates_and_spreads():
    plan = leds.blink_plan(2, 1, 100.0)
    assert [c for _, c in plan] == ["red", "blue", "red"]
    assert [t for t, _ in plan] == [100.0, 101.0, 102.0]


def test_blink_plan_caps_a_burst():
    plan = leds.blink_plan(50, 3, 0.0)
    assert len(plan) == leds.MAX_BLINKS
    assert [c for _, c in plan].count("blue") == 3
    assert plan[-1][0] < leds.POLL


def test_blink_plan_nothing_new():
    assert leds.blink_plan(0, 0, 5.0) == []
