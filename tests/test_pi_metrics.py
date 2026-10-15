"""pi/metrics/pi-metrics.py's Bluetooth dongle check (the script has a dash in
its name, so it's loaded by path)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "pi_metrics", Path(__file__).resolve().parent.parent / "pi" / "metrics" / "pi-metrics.py"
)
pi_metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pi_metrics)


def test_usb_dongle_is_1(tmp_path):
    uevent = tmp_path / "uevent"
    uevent.write_text("DRIVER=btusb\nPRODUCT=a12/1/8891\n")
    assert pi_metrics.bt_dongle(str(uevent)) == 1


def test_onboard_chip_is_0(tmp_path):
    uevent = tmp_path / "uevent"
    uevent.write_text("DRIVER=hci_uart_bcm\n")
    assert pi_metrics.bt_dongle(str(uevent)) == 0


def test_no_adapter_is_no_data(tmp_path):
    assert pi_metrics.bt_dongle(str(tmp_path / "missing")) is None
