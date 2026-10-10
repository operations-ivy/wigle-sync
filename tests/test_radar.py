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


def kml(*nets):
    """A KML with one placemark per (netid, lat, lon)."""
    marks = "".join(
        f"<Placemark><name>n</name><description>Network ID: {n}&lt;br/&gt;Time: 2026-10-09T21:15:02.000-04:00"
        f"&lt;br/&gt;Type: WIFI</description><Point><coordinates>{lon},{lat}</coordinates></Point></Placemark>"
        for n, lat, lon in nets)
    return f"<kml><Document>{marks}</Document></kml>"


def test_kml_keeps_only_position_time_type_and_an_opaque_key():
    points = radar.parse_kml(KML)
    assert [(p[0], p[1], p[3]) for p in points] == [(40.009, -75.0, "wifi"), (40.0, -74.98826, "bt")]
    assert points[0][2] == datetime.fromisoformat("2026-10-09T21:15:02-04:00").timestamp()
    assert points[0][4] == radar._key("aa:bb:cc:dd:ee:ff")  # case doesn't matter
    assert "Secret" not in repr(points) and "AA:BB" not in repr(points)


def test_center_parsing():
    assert radar.parse_center("40.1, -75.2") == (40.1, -75.2)
    assert radar.parse_center("") is None
    assert radar.parse_center("91,0") is None


def test_drive_dates_come_from_the_transid():
    assert radar.drive_date("20261009-01234") == "2026-10-09"
    assert radar.drive_date("weird") == ""


def test_projection_bearing_range_and_a_round_radius():
    # About 990 m north and 990 m east: inside the 1 km ring.
    points = [(40.0089, -75.0, 100.0, "wifi", 0, True), (40.0, -74.98838, 0.0, "bt", 1, False)]
    out = radar.project(points, (40.0, -75.0), now=200.0)
    assert out["radius_m"] == 1000
    (b1, r1, age1, k1, d1, n1), (b2, r2, age2, k2, d2, n2) = out["contacts"]
    assert abs(b1) < 0.5 and abs(r1 - 0.99) < 0.01 and age1 == 100 and (k1, d1, n1) == ("wifi", 0, 1)
    assert abs(b2 - 90) < 0.5 and age2 is None and (k2, d2, n2) == ("bt", 1, 0)


def test_radius_holds_ninety_percent_and_drops_the_far_ones():
    near = [(40.0 + i * 0.0001, -75.0, 0.0, "wifi", 0, False) for i in range(10)]  # within ~100 m
    far = [(41.0, -75.0, 0.0, "wifi", 0, False)]  # ~111 km out
    out = radar.project(near + far, (40.0, -75.0), now=0)
    assert out["radius_m"] == 500
    assert len(out["contacts"]) == 10


def test_ghost_cells_merge_neighbours_and_skip_what_is_off_the_scope():
    cells = {(40.0050, -75.0), (40.00501, -75.0), (40.0, -74.99), (41.0, -75.0)}
    out = radar.ghost(cells, (40.0, -75.0), radius_m=1000)
    assert len(out) == 2  # the two neighbours merge; 111 km is off the scope
    assert all(0 <= b < 360 and 0 <= r <= 1 for b, r in out)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def fake_wigle(uploads, kmls, calls):
    def get(url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        if url.endswith("/transactions"):
            return FakeResponse({"results": uploads})
        # WiGLE answers 406 to a KML request that only accepts JSON.
        assert kwargs["headers"]["Accept"] != "application/json"
        return FakeResponse(kmls[name])
    return get


def done(transid):
    return {"transid": transid, "status": "D", "totalGps": 5}


def test_recent_drives_in_full_older_ones_as_ghosts_a_few_at_a_time():
    uploads = [done("20261010-3"), {"transid": "20261009-x", "status": "W", "totalGps": 5},
               done("20261008-2"), done("20261007-1"), done("20261006-0")]
    kmls = {t: kml((t, 40.0, -75.0)) for t in ("20261010-3", "20261008-2", "20261007-1", "20261006-0")}
    calls = []
    r = radar.Radar(("name", "token"))
    with (mock.patch.object(radar.requests, "get", side_effect=fake_wigle(uploads, kmls, calls)),
          mock.patch.object(radar, "RADAR_UPLOADS", 2), mock.patch.object(radar, "FETCHES_PER_REFRESH", 1)):
        r.refresh()
        # Both recent drives, but only one older one this time round.
        assert calls == ["transactions", "20261010-3", "20261008-2", "20261007-1"]
        r.refresh()
        assert calls[4:] == ["transactions", "20261006-0"]  # nothing fetched twice
        assert set(r._recent) == {"20261010-3", "20261008-2"}
        assert set(r._ghost) == {"20261007-1", "20261006-0"}
        # A new drive pushes the oldest recent one down to the ghost layer.
        uploads.insert(0, done("20261011-4"))
        kmls["20261011-4"] = kml(("20261011-4", 40.0, -75.0))
        r.refresh()
    assert set(r._recent) == {"20261011-4", "20261010-3"}
    assert "20261008-2" in r._ghost


def test_a_network_is_new_only_on_the_first_drive_that_saw_it():
    uploads = [done("20261010-2"), done("20261009-1"), done("20261008-0")]
    kmls = {
        "20261008-0": kml(("old", 40.0, -75.0)),                                     # ghost layer
        "20261009-1": kml(("old", 40.0, -75.0), ("mid", 40.001, -75.0)),
        "20261010-2": kml(("mid", 40.001, -75.0), ("fresh", 40.002, -75.0)),
    }
    r = radar.Radar(("name", "token"))
    with (mock.patch.object(radar.requests, "get", side_effect=fake_wigle(uploads, kmls, [])),
          mock.patch.object(radar, "RADAR_UPLOADS", 2)):
        r.refresh()
    snap = r.snapshot(now=1791595202.0)
    assert snap["drives"] == [{"date": "2026-10-10", "contacts": 2, "new": 1},
                              {"date": "2026-10-09", "contacts": 2, "new": 1}]
    assert sum(c[5] for c in snap["contacts"]) == 2
    assert snap["ghost_drives"] == 1 and snap["ghost"]
    out = json.dumps(snap)
    assert "fresh" not in out and "mid" not in out and str(radar._key("fresh")) not in out


def test_empty_radar_is_still_a_valid_answer():
    snap = radar.Radar(("a", "b")).snapshot()
    assert (snap["contacts"], snap["drives"], snap["ghost"], snap["total"]) == ([], [], [], 0)
