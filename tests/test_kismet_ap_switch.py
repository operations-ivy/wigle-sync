"""pi/ap/kismet-ap-switch: when the onboard radio becomes
the phone AP and when it goes back to home Wi-Fi (loaded by path: no .py)."""

from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import sys
import threading
import urllib.parse
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parent.parent / "pi" / "ap" / "kismet-ap-switch"
LOADER = importlib.machinery.SourceFileLoader("kismet_ap_switch", str(PATH))
SPEC = importlib.util.spec_from_loader("kismet_ap_switch", LOADER)
ap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ap  # dataclasses look their module up
LOADER.exec_module(ap)


def test_off_home_long_enough_starts_the_ap():
    s = ap.Switch()
    assert s.step(0, home=False, ap_up=False, home_signal=None) is None
    assert s.step(ap.LEFT_AFTER - 1, home=False, ap_up=False, home_signal=None) is None
    assert s.step(ap.LEFT_AFTER, home=False, ap_up=False, home_signal=None) == "up"


def test_a_blip_off_home_wifi_starts_nothing():
    s = ap.Switch()
    s.step(0, home=False, ap_up=False, home_signal=None)
    assert s.step(60, home=True, ap_up=False, home_signal=None) is None
    assert s.step(ap.LEFT_AFTER + 30, home=False, ap_up=False, home_signal=None) is None  # the clock restarted


def test_home_heard_strongly_twice_stops_the_ap():
    s = ap.Switch()
    assert s.step(0, home=False, ap_up=True, home_signal=-50) is None
    assert s.step(20, home=False, ap_up=True, home_signal=-50) == "down"


@pytest.mark.parametrize("signal", [None, ap.HOME_SIGNAL - 1])
def test_home_faint_or_unheard_keeps_the_ap(signal):
    s = ap.Switch()
    assert [s.step(t, home=False, ap_up=True, home_signal=signal) for t in (0, 20, 40)] == [None, None, None]


def test_a_miss_in_between_starts_the_count_again():
    s = ap.Switch()
    s.step(0, home=False, ap_up=True, home_signal=-40)
    s.step(20, home=False, ap_up=True, home_signal=None)
    assert s.step(40, home=False, ap_up=True, home_signal=-40) is None


def test_a_failed_rejoin_brings_the_ap_back_at_once_and_waits_before_home_again():
    s = ap.Switch()
    s.rejoin_failed(1000)
    assert s.step(1000, home=False, ap_up=False, home_signal=None) == "up"
    s.step(1020, home=False, ap_up=True, home_signal=-40)
    assert s.step(1040, home=False, ap_up=True, home_signal=-40) is None  # cooling down
    assert s.step(1000 + ap.COOLDOWN, home=False, ap_up=True, home_signal=-40) == "down"  # heard all along


def test_settings_are_read_like_the_shell_does(tmp_path):
    f = tmp_path / "kismet-pi"
    f.write_text('# a comment\nHOME_WIFI_SSID="home net"\nKISMET_AP_SSID=car\nnot a setting\n')
    assert ap.read_settings(f) == {"HOME_WIFI_SSID": "home net", "KISMET_AP_SSID": "car"}


@pytest.fixture
def login(tmp_path):
    path = tmp_path / "kismet-httpd"
    path.write_text("httpd_username=wigle\nhttpd_password=secret\n")
    return path


@pytest.fixture
def kismet(login):
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["auth"] = self.headers["Authorization"]
            seen["path"] = self.path
            body = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            seen["fields"] = json.loads(body["json"][0])["fields"]
            out = json.dumps(
                [
                    {"kismet.device.base.commonname": "home net", "signal": -61},
                    {"kismet.device.base.commonname": "home net", "signal": -44},
                    {"kismet.device.base.commonname": "neighbour", "signal": -30},
                ]
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", login, seen
    server.shutdown()


def test_kismet_hears_gives_the_strongest_home_signal(kismet):
    base, login, seen = kismet
    assert ap.kismet_hears("home net", login, base) == -44
    assert seen["path"] == f"/devices/views/phydot11_accesspoints/last-time/-{ap.SEEN_WITHIN}/devices.json"
    assert seen["auth"] == "Basic d2lnbGU6c2VjcmV0"


def test_kismet_not_running_is_kismet_down(login):
    with pytest.raises(ap.KismetDown):
        ap.kismet_hears("home net", login, "http://127.0.0.1:9")


def test_no_login_is_kismet_down(tmp_path):
    with pytest.raises(ap.KismetDown):
        ap.kismet_hears("home net", tmp_path / "missing", "http://127.0.0.1:9")


def test_kismet_silent_for_three_minutes_tries_home():
    # Kismet stopped (at home, 15 minutes after a boot) while the AP is up: try home.
    s = ap.Switch()
    out = [s.step(20 * i, home=False, ap_up=True, home_signal=None, kismet_up=False) for i in range(ap.UNANSWERED)]
    assert out[:-1] == [None] * (ap.UNANSWERED - 1)
    assert out[-1] == "down"


def test_kismet_answering_again_resets_the_silence():
    s = ap.Switch()
    for i in range(ap.UNANSWERED - 1):
        s.step(20 * i, home=False, ap_up=True, home_signal=None, kismet_up=False)
    s.step(500, home=False, ap_up=True, home_signal=None, kismet_up=True)
    assert s.step(520, home=False, ap_up=True, home_signal=None, kismet_up=False) is None


def test_an_error_object_from_kismet_is_kismet_down(login):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            out = b'{"error": "busy"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ap.KismetDown):
            ap.kismet_hears("home net", login, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
