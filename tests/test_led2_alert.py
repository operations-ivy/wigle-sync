"""pi/leds/led2-alert.py's matching and breathing (loaded by path: the script
has a dash in its name and imports detect from beside it)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

LEDS = Path(__file__).resolve().parent.parent / "pi" / "leds"
sys.path.insert(0, str(LEDS))
SPEC = importlib.util.spec_from_file_location("led2_alert", LEDS / "led2-alert.py")
led2 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(led2)


def device(mac, name, phy="IEEE802.11"):
    return {
        "kismet.device.base.macaddr": mac,
        "kismet.device.base.commonname": name,
        "kismet.device.base.phyname": phy,
    }


def test_flock_ssid_alerts():
    assert led2.kinds_near([device("B4:1E:52:00:00:01", "Flock-1A2B3C")]) == {"flock"}


def test_flock_name_on_bluetooth_does_not():
    assert led2.kinds_near([device("B4:1E:52:00:00:01", "Flock-1A2B3C", "Bluetooth")]) == set()


def test_axon_oui_alerts_on_either_phy():
    assert led2.kinds_near([device("00:25:DF:12:34:56", "", "Bluetooth")]) == {"axon"}
    assert led2.kinds_near([device("00:25:df:12:34:56", "x")]) == {"axon"}


def test_flipper_and_ordinary_devices_do_not():
    assert led2.kinds_near([device("AA:BB:CC:00:00:01", "Flipper Bob", "Bluetooth"), device("AA:BB:CC:00:00:02", "HomeNet")]) == set()


def test_both_kinds():
    assert led2.kinds_near([device("00:25:DF:12:34:56", ""), device("B4:1E:52:00:00:01", "Flock-ABCD")]) == {"axon", "flock"}


def test_credentials(tmp_path):
    conf = tmp_path / "kismet_httpd.conf"
    conf.write_text("httpd_username=wigle\nhttpd_password=abc=def\n")
    assert led2.credentials(conf) == ("wigle", "abc=def")


def test_breath_rises_and_falls():
    b = led2.BREATH
    assert led2.breath(0) == 0
    assert led2.breath(b / 2) == 1
    assert 0 < led2.breath(b / 4) < 1
    assert abs(led2.breath(b / 4) - led2.breath(3 * b / 4)) < 1e-9
    assert led2.breath(b) == 0


def test_green_writes_duty_only_on_change(tmp_path):
    pwm = tmp_path / "pwm0"
    pwm.mkdir()
    for f in ("duty_cycle", "period", "enable"):
        (pwm / f).write_text("")
    green = led2.Green(tmp_path)
    assert (pwm / "period").read_text() == str(led2.PERIOD)
    assert (pwm / "enable").read_text() == "1"
    green.set(0.5)
    assert (pwm / "duty_cycle").read_text() == str(led2.PERIOD // 2)
    (pwm / "duty_cycle").write_text("untouched")
    green.set(0.5)
    assert (pwm / "duty_cycle").read_text() == "untouched"
    green.set(2)
    assert (pwm / "duty_cycle").read_text() == str(led2.PERIOD)
