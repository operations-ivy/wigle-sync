"""pi/leds/tidy-config.awk: the old LED lines and their comments out of config.txt."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

AWK = Path(__file__).resolve().parent.parent / "pi" / "leds" / "tidy-config.awk"
MARK = "# wigle-sync: RGB status LEDs (pi/leds/setup-leds.sh)"
OLD = ["gpio=5,13,21,27=op,dl", "dtoverlay=pwm-gpio,gpio=6", "dtoverlay=pwm-gpio,gpio=22"]


def tidy(tmp_path, text, old=OLD):
    config = tmp_path / "config.txt"
    config.write_text(text)
    env = {**os.environ, "OLD": "\n".join(old) + "\n"}
    result = subprocess.run(["awk", "-v", f"MARK={MARK}", "-f", str(AWK), str(config)], env=env, capture_output=True, text=True, check=True)
    return result.stdout


def test_old_lines_go_with_their_comments(tmp_path):
    text = f"[all]\n{MARK}\ndtoverlay=pwm-gpio,gpio=6\n{MARK}\ndtoverlay=pwm-gpio,gpio=17\n{MARK}\ngpio=5,13,21,27=op,dl\n"
    assert tidy(tmp_path, text) == f"[all]\n{MARK}\ndtoverlay=pwm-gpio,gpio=17\n"


def test_leftover_comments_from_earlier_runs(tmp_path):
    # brick69's config.txt on 2026-10-23, after the first heat-LED install.
    text = (
        "[all]\n\n"
        "# wigle-sync: onboard Bluetooth off; a USB dongle is hci0 (pi/bootstrap.sh)\ndtoverlay=disable-bt\n"
        f"{MARK}\ndtoverlay=gpio-led,gpio=16,label=led1-red,trigger=actpwr\n"
        "dtoverlay=gpio-led,gpio=20,label=led1-green,trigger=mmc0\n"
        f"{MARK}\n{MARK}\n{MARK}\ndtoverlay=pwm-gpio,gpio=17\n"
        f"{MARK}\n{MARK}\n{MARK}\ndtoverlay=pwm-gpio,gpio=27\n"
        f"{MARK}\ngpio=5,6,13,21,22=op,dl\n"
    )
    assert tidy(tmp_path, text) == (
        "[all]\n\n"
        "# wigle-sync: onboard Bluetooth off; a USB dongle is hci0 (pi/bootstrap.sh)\ndtoverlay=disable-bt\n"
        f"{MARK}\ndtoverlay=gpio-led,gpio=16,label=led1-red,trigger=actpwr\n"
        "dtoverlay=gpio-led,gpio=20,label=led1-green,trigger=mmc0\n"
        f"{MARK}\ndtoverlay=pwm-gpio,gpio=17\n"
        f"{MARK}\ndtoverlay=pwm-gpio,gpio=27\n"
        f"{MARK}\ngpio=5,6,13,21,22=op,dl\n"
    )


def test_a_comment_at_the_end(tmp_path):
    assert tidy(tmp_path, f"[all]\n{MARK}\n") == "[all]\n"


def test_blank_lines_and_other_comments_stay(tmp_path):
    text = "# a comment\n\n[all]\n\ndtparam=audio=on\n"
    assert tidy(tmp_path, text) == text
    assert tidy(tmp_path, text, old=[]) == text


def test_tidy_twice_changes_nothing(tmp_path):
    once = tidy(tmp_path, f"{MARK}\n{MARK}\ndtoverlay=pwm-gpio,gpio=17\n{MARK}\ndtoverlay=pwm-gpio,gpio=22\n")
    assert tidy(tmp_path, once) == once == f"{MARK}\ndtoverlay=pwm-gpio,gpio=17\n"
