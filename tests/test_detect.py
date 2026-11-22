"""detect.py: surveillance and tracking devices in a drive's .wiglecsv.
Every address and name here is made up."""

from __future__ import annotations

from collections import Counter

import detect

HEADER = (
    "WigleWifi-1.4,appRelease=Kismet\n"
    "MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n"
)


def row(mac, name, kind="WIFI"):
    return f"{mac},{name},[WPA2],2026-10-20 18:00:00,6,-60,0.0,0.0,0,5,{kind}\n"


def test_each_kind_and_each_address_once():
    text = HEADER + "".join(
        [
            row("12:34:56:78:9a:bc", "Flock-A1B2C3"),
            row("12:34:56:78:9a:bc", "Flock-A1B2C3"),  # the same camera heard twice
            row("12:34:56:78:9a:bd", "Flock-0F0F0F"),
            row("00:25:df:11:22:33", "AXON-CAM", "BLE"),
            row("00:25:df:44:55:66", "", "WIFI"),
            row("aa:bb:cc:00:00:01", "Flipper Gizmo", "BLE"),
            row("aa:bb:cc:00:00:02", "HC-05", "BT"),
            row("aa:bb:cc:00:00:03", "Neighbours", "WIFI"),
            row("aa:bb:cc:00:00:04", "Pixel 9", "BLE"),
        ]
    )
    found = detect.scan_text(text)
    assert found == Counter({"flock": 2, "axon": 2, "flipper": 1, "skimmer": 1})
    assert detect.alerting(found) == Counter({"flock": 2, "axon": 2, "flipper": 1})  # the skimmer guess is low


def test_near_misses():
    assert detect.kind_of("02:25:df:11:22:33", "", "BLE") is None  # Axon's OUI in a locally set address
    assert detect.kind_of("aa:bb:cc:00:00:05", "Flock-", "WIFI") is None  # no serial
    assert detect.kind_of("aa:bb:cc:00:00:06", "Flock Party", "WIFI") is None
    assert detect.kind_of("aa:bb:cc:00:00:07", "Flipper Gizmo", "WIFI") is None  # a Wi-Fi SSID, not a Flipper
    assert detect.kind_of("aa:bb:cc:00:00:08", "Flock-ABCD", "BLE") is None


def test_flock_signatures():
    """The signatures flock-you and O'Horo's write-up agree on (wigle-sync#85)."""
    assert detect.kind_of("b4:1e:52:12:34:56", "", "WIFI") == "flock"  # Flock's own OUI
    assert detect.kind_of("B4:1E:52:12:34:56", "", "BLE") == "flock"
    assert detect.kind_of("aa:bb:cc:12:34:56", "Flock Camera net 7", "WIFI") == "flock"
    assert detect.kind_of("C1:22:33:44:55:66", "FS Ext Battery", "BLE") == "flock"
    assert detect.kind_of("C1:22:33:44:55:66", "Penguin-1234567890", "BT") == "flock"
    assert detect.kind_of("C1:22:33:44:55:66", "1234567890", "BLE", company="09C8") == "flock"
    assert detect.kind_of("C1:22:33:44:55:66", "", "BLE", ["E8CCBB38-9532-46A8-9FE5-1814DF172E6F"]) == "flock"


def test_raven_and_shotspotter():
    assert detect.kind_of("D1:22:33:44:55:66", "", "BLE", ["3100", "3500"]) == "raven"
    assert detect.kind_of("D1:22:33:44:55:66", "", "BLE", ["3100"]) is None  # one 16-bit UUID can be anyone's
    assert detect.kind_of("D4:11:D6:12:34:56", "", "WIFI") == "shotspotter"
    assert detect.alerting(Counter({"raven": 1, "shotspotter": 1})) == Counter({"raven": 1, "shotspotter": 1})


def test_what_ordinary_devices_look_like_does_not_match():
    """Each of these is a kind of device seen on our own drives."""
    for mac, name, kind in [
        ("AA:BB:CC:00:00:01", "64WB-Flock", "WIFI"),  # a substring
        ("AA:BB:CC:00:00:02", "PenguinGuest", "WIFI"),  # a home network
        ("C1:22:33:44:55:66", "1234567890", "BLE"),  # 10 digits, without Flock's company ID
        ("C1:22:33:44:55:66", "Penguin-12345", "BLE"),  # too short a serial
        ("74:4C:A1:00:00:03", "", "WIFI"),  # a Liteon OUI, as in Flock's radios and many laptops
        ("C1:22:33:44:55:67", "Raven 2RVP012345678", "BLE"),  # unconfirmed: no services to go on
    ]:
        assert detect.kind_of(mac, name, kind) is None, name


def test_the_led_kinds_are_kinds():
    assert detect.LED2_KINDS <= set(detect.KINDS)
    assert "flipper" not in detect.LED2_KINDS


def test_header_only_and_other_files(tmp_path):
    assert detect.scan_text(HEADER) == Counter()
    kismet = tmp_path / "Kismet-1.kismet"
    kismet.write_bytes(b"SQLite format 3\x00")
    assert detect.scan(kismet) == Counter()
    csv = tmp_path / "Kismet-1.wiglecsv"
    csv.write_text(HEADER + row("aa:bb:cc:00:00:01", "Flipper Gizmo", "BLE"))
    assert detect.scan(csv) == Counter({"flipper": 1})


def test_rows_without_a_type_are_wifi():
    text = "WigleWifi-1.4\nMAC,SSID\n12:34:56:78:9a:bc,Flock-A1B2C3\n"
    assert detect.scan_text(text) == Counter({"flock": 1})
