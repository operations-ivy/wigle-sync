from __future__ import annotations

import json
from datetime import datetime
from unittest import mock

import radar

# Shaped like WiGLE's /file/kml output: a name, a description with the fields,
# and lon,lat coordinates. The names and IDs here must never reach the output.
KML = """<?xml version="1.0"?><kml><Document>
<Placemark><name>SecretHomeWifi</name><open>1</open>
<description><![CDATA[Network ID: <b>AA:BB:CC:DD:EE:FF</b><br/>Encryption: WPA2<br/>Time: 2026-10-09T21:15:02.000-04:00<br/>Signal: -70.0<br/>Accuracy: 4.0<br/>Type: WIFI]]></description>
<styleUrl>#highConfidence</styleUrl><Point><coordinates>-75.000000,40.009000</coordinates></Point></Placemark>
<Placemark><name>Headphones</name>
<description>Network ID: 11:22:33:44:55:66&lt;br/&gt;Time: 2026-10-09T21:16:00.000-04:00&lt;br/&gt;Type: BT</description>
<Point><coordinates>-74.988260,40.000000</coordinates></Point></Placemark>
<Placemark><name>no position</name><description>Type: WIFI</description></Placemark>
</Document></kml>"""


def test_kml_keeps_only_position_time_and_type():
    points = radar.parse_kml(KML)
    assert [(p[0], p[1], p[3]) for p in points] == [(40.009, -75.0, "wifi"), (40.0, -74.98826, "bt")]
    assert points[0][2] == datetime.fromisoformat("2026-10-09T21:15:02-04:00").timestamp()
    assert "Secret" not in repr(points) and "AA:BB" not in repr(points)


def test_center_parsing():
    assert radar.parse_center("40.1, -75.2") == (40.1, -75.2)
    assert radar.parse_center("") is None
    assert radar.parse_center("91,0") is None


def test_projection_bearing_range_and_a_round_radius():
    # About 990 m north and 990 m east: inside the 1 km ring.
    points = [(40.0089, -75.0, 100.0, "wifi"), (40.0, -74.98838, 0.0, "bt")]
    out = radar.project(points, (40.0, -75.0), now=200.0)
    assert out["radius_m"] == 1000
    (b1, r1, age1, k1), (b2, r2, age2, k2) = out["contacts"]
    assert abs(b1) < 0.5 and abs(r1 - 0.99) < 0.01 and age1 == 100 and k1 == "wifi"
    assert abs(b2 - 90) < 0.5 and age2 is None and k2 == "bt"


def test_radius_holds_ninety_percent_and_drops_the_far_ones():
    near = [(40.0 + i * 0.0001, -75.0, 0.0, "wifi") for i in range(10)]  # within ~100 m
    far = [(41.0, -75.0, 0.0, "wifi")]  # ~111 km out
    out = radar.project(near + far, (40.0, -75.0), now=0)
    assert out["radius_m"] == 500
    assert len(out["contacts"]) == 10


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def test_refresh_fetches_each_upload_once_and_forgets_old_ones():
    calls = []
    uploads = {"results": [{"transid": "t3", "status": "D", "totalGps": 5},
                           {"transid": "t2", "status": "W", "totalGps": 5},  # still processing
                           {"transid": "t1", "status": "D", "totalGps": 0},  # nothing located
                           {"transid": "t0", "status": "D", "totalGps": 9}]}

    def fake_get(url, **kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        # WiGLE answers 406 to a KML request that only accepts JSON.
        if "/kml/" in url:
            assert kwargs["headers"]["Accept"] != "application/json"
        return FakeResponse(uploads if url.endswith("/transactions") else KML)

    r = radar.Radar(("name", "token"))
    with mock.patch.object(radar.requests, "get", side_effect=fake_get), mock.patch.object(radar, "RADAR_UPLOADS", 1):
        r.refresh()
        assert calls == ["transactions", "t3"]
        r.refresh()
        assert calls == ["transactions", "t3", "transactions"]  # t3 not fetched again
        uploads["results"].insert(0, {"transid": "t4", "status": "D", "totalGps": 2})
        r.refresh()
    assert set(r._kml) == {"t4"}
    snap = r.snapshot(now=1791595202.0)
    assert snap["drives"] == 1 and snap["total"] == 2 and snap["centered"] == "median"
    assert "Secret" not in json.dumps(snap)


def test_empty_radar_is_still_a_valid_answer():
    assert radar.Radar(("a", "b")).snapshot() == {"radius_m": 1000, "contacts": [], "drives": 0, "total": 0,
                                                 "centered": "none"}
