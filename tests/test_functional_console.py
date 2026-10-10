"""The console app end to end: the real Flask app, with its background
threads, against one stub server playing Prometheus, Loki and WiGLE."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlsplit

import pytest

KML = ("<kml><Document><Placemark><name>SecretNet</name><description>Network ID: AA:BB:CC:DD:EE:FF"
       "&lt;br/&gt;Time: 2026-10-09T21:15:02.000-04:00&lt;br/&gt;Type: WIFI</description>"
       "<Point><coordinates>-75.0,40.0</coordinates></Point></Placemark></Document></kml>")
SYNC_LINE = json.dumps({"event": "Sync complete", "pi_online": True, "files_found": 2, "uploaded": 1,
                        "archived_empty": 0, "archived_kept": 1, "failed": 0, "deferred": 0})


class Upstreams(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/v2/file/kml/"):
            # Like WiGLE: a KML request that only accepts JSON gets a 406.
            if self.headers.get("Accept") == "application/json":
                return self.reply(406, b"{}")
            return self.reply(200, KML.encode(), "application/vnd.google-earth.kml+xml")
        body = {
            "/api/v1/query": {"status": "success", "data": {"resultType": "vector", "result": []}},
            "/loki/api/v1/query_range": {"data": {"result": [{"stream": {}, "values": [[str(time.time_ns()), SYNC_LINE]]}]}},
            "/api/v2/stats/user": {"statistics": {"userName": "tester", "rank": 1}},
            "/api/v2/file/transactions": {"results": [{"transid": "20261009-00001", "status": "D", "totalGps": 1,
                                                       "fileName": "Kismet-1.wiglecsv.tar.gz"}]},
        }.get(path)
        self.reply(200 if body else 404, json.dumps(body or {}).encode())

    def reply(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def client():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstreams)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    env = {"WIGLE_API_NAME": "n", "WIGLE_API_TOKEN": "t", "PROMETHEUS_URL": base, "LOKI_URL": base,
           "PI_HOST": "127.0.0.1", "PI_PORT": "1"}
    with mock.patch.dict(os.environ, env):
        import radar
        import stats
        # stats and radar bind WiGLE's URL at import; point both at the stub
        # before console starts their background threads.
        with (mock.patch.object(radar, "WIGLE_API_URL", f"{base}/api/v2"),
              mock.patch.object(stats, "WIGLE_API_URL", f"{base}/api/v2"),
              mock.patch.object(stats, "PROMETHEUS_URL", base), mock.patch.object(stats, "LOKI_URL", base)):
            import console
            yield console.app.test_client()
    server.shutdown()


def wait_for(fn, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def test_health_and_readiness(client):
    assert client.get("/health").status_code == 200
    assert wait_for(lambda: client.get("/ready").status_code == 200), "never became ready"


def test_radar_api_has_the_drive_and_no_names(client):
    assert wait_for(lambda: client.get("/api/radar").get_json()["drives"])
    snap = client.get("/api/radar").get_json()
    assert snap["drives"] == [{"date": "2026-10-09", "contacts": 1, "new": 1}]
    assert snap["total"] == 1
    body = client.get("/api/radar").get_data(as_text=True)
    assert "SecretNet" not in body and "AA:BB" not in body


def test_the_radar_page_is_served_on_its_own_name(client):
    assert b"WARDRIVE RADAR" in client.get("/", headers={"Host": "radar.brick.nozdormu.cloud"}).data
    assert b"WARDRIVE RADAR" in client.get("/radar").data
    assert b"WARDRIVE RADAR" not in client.get("/", headers={"Host": "wigle.brick.nozdormu.cloud"}).data


def test_status_api_reads_loki_and_wigle(client):
    assert wait_for(lambda: (client.get("/api/status").get_json().get("history") or {}).get("runs"))
    status = client.get("/api/status").get_json()
    [run] = status["history"]["runs"]
    assert (run["uploaded"], run["archived_kept"]) == (1, 1)
    assert status["wigle"]["user"] == "tester"
