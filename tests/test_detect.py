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
