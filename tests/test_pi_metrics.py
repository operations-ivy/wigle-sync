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


def test_kismet_login_reads_kismets_own_conf(tmp_path):
    conf = tmp_path / "kismet_httpd.conf"
    conf.write_text("httpd_username=kis\nhttpd_password=pw=with=equals\n")
    assert pi_metrics.kismet_login(conf) == ("kis", "pw=with=equals")
    assert pi_metrics.kismet_login(tmp_path / "missing") is None


def test_sources_by_name(monkeypatch):
    raw = [
        {
            "kismet.datasource.name": "usb_bt",
            "kismet.datasource.running": 1,
            "kismet.datasource.error": 0,
            "kismet.datasource.total_retry_attempts": 2,
            "kismet.datasource.num_packets": 2995,
        },
        {"kismet.datasource.name": "wlan_mon0", "kismet.datasource.running": 0, "kismet.datasource.error": 1},
    ]
    monkeypatch.setattr(pi_metrics, "kismet_get", lambda *a, **k: raw)
    assert pi_metrics.kismet_sources(("u", "p")) == {
        "usb_bt": {"running": 1, "error": 0, "retries": 2, "packets": 2995},
        "wlan_mon0": {"running": 0, "error": 1, "retries": 0, "packets": 0},
    }


def test_recent_devices_by_phy(monkeypatch):
    phys = ["IEEE802.11", "IEEE802.11", "Bluetooth", "BTLE", "RTL433"]
    monkeypatch.setattr(pi_metrics, "kismet_get", lambda *a, **k: [{"kismet.device.base.phyname": p} for p in phys])
    assert pi_metrics.kismet_recent(("u", "p")) == {"wifi": 2, "bluetooth": 2, "other": 1}
    monkeypatch.setattr(pi_metrics, "kismet_get", lambda *a, **k: None)
    assert pi_metrics.kismet_recent(("u", "p")) is None


def test_render_labels_each_source_and_phy(monkeypatch):
    for name, value in {
        "kismet_log_bytes": 1024,
        "pending_logs": (0, 0),
        "gps": (3, 7),
        "bt_dongle": 1,
        "home": 0,
        "throttled": 0,
        "kismet_login": ("u", "p"),
    }.items():
        monkeypatch.setattr(pi_metrics, name, lambda *a, v=value, **k: v)
    monkeypatch.setattr(
        pi_metrics, "kismet_sources", lambda login: {"usb_bt": {"running": 1, "error": 0, "retries": 0, "packets": 9}}
    )
    monkeypatch.setattr(pi_metrics, "kismet_recent", lambda login: {"wifi": 4, "bluetooth": 0})
    text = pi_metrics.render()
    assert 'pi_kismet_source_running{source="usb_bt"} 1' in text
    assert 'pi_kismet_source_packets{source="usb_bt"} 9' in text
    assert 'pi_kismet_recent_devices{phy="bluetooth"} 0' in text
    assert text.count("# TYPE pi_kismet_recent_devices gauge") == 1


def test_no_kismet_no_source_series(monkeypatch):
    for name, value in {
        "kismet_log_bytes": None,
        "pending_logs": (0, 0),
        "gps": (0, 0),
        "bt_dongle": 1,
        "home": 1,
        "throttled": 0,
    }.items():
        monkeypatch.setattr(pi_metrics, name, lambda *a, v=value, **k: v)
    assert "pi_kismet_source" not in pi_metrics.render()
