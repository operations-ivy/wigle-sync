"""The home-wifi scripts on the Pi: a blip at home is not an outing.

50-kismet-home (NetworkManager dispatcher), kismet-left-home and
kismet-home-stop run here against stubbed systemctl, systemd-run, nmcli,
on-home-wifi and sleep, which record calls and keep timer state in files.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOTFS = Path(__file__).resolve().parent.parent / "pi" / "rootfs"
DISPATCHER = ROOTFS / "etc/NetworkManager/dispatcher.d/50-kismet-home"
LEFT_HOME = ROOTFS / "usr/local/sbin/kismet-left-home"
HOME_STOP = ROOTFS / "usr/local/sbin/kismet-home-stop"

HOME_SSID = "home-net"
STOP_RETRIES = 30  # kismet-home-stop: 30 tries 5 s apart for home wifi to come back

STUBS = {
    # Active units live one per line in $STATE/active.
    "bin/systemctl": """#!/bin/sh
echo "systemctl $*" >> "$STATE/calls"
case "$1" in
  is-active) [ "$2" = --quiet ] && shift; grep -qx "$2" "$STATE/active"; exit ;;
  stop) grep -vx "$2" "$STATE/active" > "$STATE/active.new"; mv "$STATE/active.new" "$STATE/active" ;;
  start) echo "$2" >> "$STATE/active" ;;
  show)
    case "$3" in
      SubState) grep -qx "$5" "$STATE/active" && echo waiting || echo dead ;;
      *) echo 0 ;;
    esac ;;
esac
exit 0
""",
    "bin/systemd-run": """#!/bin/sh
echo "systemd-run $*" >> "$STATE/calls"
for a in "$@"; do case "$a" in --unit=*) echo "${a#--unit=}.timer" >> "$STATE/active" ;; esac; done
""",
    "bin/nmcli": '#!/bin/sh\necho "$SSID"\n',
    # Each sleep is one step closer to coming home when $STATE/home_after is set.
    "bin/sleep": """#!/bin/sh
echo "sleep $*" >> "$STATE/calls"
if [ -e "$STATE/home_after" ]; then
  n=$(($(cat "$STATE/home_after") - 1)); echo "$n" > "$STATE/home_after"
  [ "$n" -le 0 ] && touch "$STATE/home"
fi
exit 0
""",
    "sbin/on-home-wifi": '#!/bin/sh\n[ -e "$STATE/home" ]\n',
}


class Pi:
    def __init__(self, root: Path):
        self.bin = root / "bin"
        self.state = root / "state"
        self.state.mkdir()
        (self.state / "active").write_text("")
        (self.state / "calls").write_text("")
        for rel, body in STUBS.items():
            path = root / rel
            path.parent.mkdir(exist_ok=True)
            path.write_text(body)
            path.chmod(0o755)
        defaults = root / "kismet-pi"
        defaults.write_text(f"HOME_WIFI_SSID={HOME_SSID}\n")
        uptime = root / "uptime"
        uptime.write_text("1000.00 900.00\n")
        self.marker = root / "left-home"
        self.env = {
            "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
            "STATE": str(self.state),
            "SSID": HOME_SSID,
            "CONNECTION_UUID": "uuid-1",
            "KISMET_SBIN": str(root / "sbin"),
            "KISMET_PI_DEFAULTS": str(defaults),
            "KISMET_UPTIME": str(uptime),
            "KISMET_LEFT_MARKER": str(self.marker),
        }

    def at_home(self, home: bool = True) -> None:
        flag = self.state / "home"
        if home:
            flag.touch()
        else:
            flag.unlink(missing_ok=True)

    def run(self, script: Path, *args: str, **env: str) -> str:
        done = subprocess.run(["sh", str(script), *args], env=self.env | env,
                              capture_output=True, text=True, check=True, timeout=10)
        return done.stdout

    def event(self, action: str) -> str:
        self.at_home(action == "up")
        return self.run(DISPATCHER, "wlan_host", action)

    @property
    def active(self) -> set[str]:
        return set((self.state / "active").read_text().split())

    @property
    def calls(self) -> list[str]:
        return (self.state / "calls").read_text().splitlines()

    def fire(self, unit: str) -> None:
        """The unit's timer elapses: systemd-run's transient timers then stop."""
        self.run(self.bin / "systemctl", "stop", f"{unit}.timer")

    def forget_calls(self) -> None:
        (self.state / "calls").write_text("")


@pytest.fixture
def pi(tmp_path: Path) -> Pi:
    return Pi(tmp_path)


def test_up_at_home_arms_the_stop_after_the_grace(pi):
    pi.event("up")
    assert "kismet-home-stop.timer" in pi.active
    assert any("--on-boot=1900" in c and c.endswith("/kismet-home-stop") for c in pi.calls)


def test_down_only_arms_the_left_home_check(pi):
    pi.event("up")
    pi.forget_calls()
    pi.event("down")
    assert any("--unit=kismet-left-home" in c and "--on-boot=1120" in c for c in pi.calls)
    assert "kismet-home-stop.timer" in pi.active, "a drop alone must not cancel the stop"
    assert "systemctl start kismet.service" not in pi.calls
    assert not pi.marker.exists()


def test_blip_changes_nothing(pi):
    pi.event("up")
    pi.forget_calls()
    pi.event("down")
    out = pi.event("up")
    assert "blip" in out
    assert "kismet-left-home.timer" not in pi.active
    assert "kismet-home-stop.timer" in pi.active
    assert not any(c.startswith("systemd-run") and "kismet-home-stop" in c for c in pi.calls), \
        "a blip must not re-arm the stop"
    assert not pi.marker.exists()


def test_blip_keeps_a_stop_cancelled_by_hand(pi):
    pi.event("up")
    pi.run(pi.bin / "systemctl", "stop", "kismet-home-stop.timer")  # by hand
    pi.event("down")
    pi.event("up")
    assert "kismet-home-stop.timer" not in pi.active


def test_back_after_the_grace_is_a_return_from_an_outing(pi):
    pi.event("down")
    pi.fire("kismet-left-home")
    pi.run(LEFT_HOME)
    pi.forget_calls()
    pi.event("up")
    assert "kismet-home-stop.timer" in pi.active
    assert pi.marker.exists()


def test_left_home_marks_the_outing_and_starts_kismet(pi):
    pi.event("up")
    pi.at_home(False)
    pi.run(LEFT_HOME)
    assert pi.marker.exists()
    assert "kismet-home-stop.timer" not in pi.active
    assert "kismet.service" in pi.active


def test_left_home_does_nothing_back_on_home_wifi(pi):
    pi.at_home()
    pi.run(LEFT_HOME)
    assert not pi.marker.exists()
    assert pi.calls == []


def test_other_networks_are_ignored(pi):
    pi.run(DISPATCHER, "wlan_host", "up", SSID="cafe")
    pi.run(DISPATCHER, "wlan_host", "down", SSID="cafe")
    pi.run(DISPATCHER, "wlan_mon0", "down")
    assert pi.calls == []


def test_home_stop_waits_out_a_blip(pi):
    pi.at_home(False)
    (pi.state / "home_after").write_text("3")
    pi.run(HOME_STOP)
    assert pi.calls.count("sleep 5") == 3
    assert "systemctl stop kismet.service" in pi.calls


def test_home_stop_gives_up_once_the_pi_has_left(pi):
    pi.at_home(False)
    pi.run(HOME_STOP)
    assert pi.calls.count("sleep 5") == STOP_RETRIES
    assert "systemctl stop kismet.service" not in pi.calls
