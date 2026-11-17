"""pi/bootstrap.sh: setup steps that depend on each other run in order."""

import re
from pathlib import Path

import pytest

PI = Path(__file__).resolve().parent.parent / "pi"

# The setup step for each directory whose unit loads Kismet's web login
# (kismet_httpd.conf) with LoadCredential: started before the file exists, the
# unit fails and the run stops (#65).
LOGIN_LOADERS = {
    "metrics": "metrics/setup-metrics.sh",
    "eink": "eink/setup-eink.sh",
    "leds": "leds/setup-leds.sh",
}


def setup_steps() -> list[str]:
    """The setup scripts bootstrap.sh runs, in order."""
    return re.findall(r'bash "\$SRC/([\w/.-]+\.sh)"', (PI / "bootstrap.sh").read_text())


@pytest.mark.parametrize("step", LOGIN_LOADERS.values())
def test_kismet_login_before_the_units_that_load_it(step):
    steps = setup_steps()
    assert step in steps, f"bootstrap.sh no longer runs {step}"
    assert steps.index("setup-kismet-login.sh") < steps.index(step)


def test_every_unit_that_loads_the_login_is_listed():
    loaders = {unit.relative_to(PI).parts[0] for unit in PI.rglob("*.service")
               if "kismet_httpd.conf" in unit.read_text()}
    assert loaders <= LOGIN_LOADERS.keys()
