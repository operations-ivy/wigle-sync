"""pi/zero2w/pi-profile: which board profile bootstrap.sh builds."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "pi" / "zero2w" / "pi-profile"


def profile(tmp_path, model, pi_profile=None):
    path = tmp_path / "model"
    if model is not None:
        path.write_bytes(model.encode() + b"\0")
    env = {"PATH": "/usr/bin:/bin", "MODEL": str(path)}
    if pi_profile is not None:
        env["PI_PROFILE"] = pi_profile
    return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize(
    "model, expected",
    [
        ("Raspberry Pi Zero 2 W Rev 1.0", "zero2w"),
        ("Raspberry Pi 3 Model B Plus Rev 1.3", "default"),
        ("Raspberry Pi Zero W Rev 1.1", "default"),
        (None, "default"),
    ],
)
def test_detected(tmp_path, model, expected):
    result = profile(tmp_path, model)
    assert result.returncode == 0
    assert result.stdout == f"{expected}\n"


@pytest.mark.parametrize(
    "model, setting, expected",
    [
        ("Raspberry Pi 3 Model B Plus Rev 1.3", "zero2w", "zero2w"),
        ("Raspberry Pi Zero 2 W Rev 1.0", "default", "default"),
        ("Raspberry Pi Zero 2 W Rev 1.0", "auto", "zero2w"),
    ],
)
def test_setting_overrides_detection(tmp_path, model, setting, expected):
    result = profile(tmp_path, model, setting)
    assert result.returncode == 0
    assert result.stdout == f"{expected}\n"


def test_unknown_setting_fails(tmp_path):
    result = profile(tmp_path, "Raspberry Pi Zero 2 W Rev 1.0", "pi5")
    assert result.returncode == 2
    assert "PI_PROFILE" in result.stderr
