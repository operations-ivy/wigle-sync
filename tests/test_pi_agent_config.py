"""pi/metrics/agent.yml: vmagent's scrape config, named for each Pi by
setup-metrics.sh."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from tests.conftest import PI

AGENT = PI / "metrics" / "agent.yml"


def rendered(name: str) -> dict:
    """agent.yml as setup-metrics.sh writes it on a Pi called `name`."""
    out = subprocess.run(["sed", f"s/PI_NAME/{name}/g", str(AGENT)], capture_output=True, text=True, check=True)
    return yaml.safe_load(out.stdout)


@pytest.mark.parametrize("name", ["brick69", "scootypuffsr"])
def test_each_pi_is_its_own_instance(name):
    jobs = rendered(name)["scrape_configs"]
    assert [j["job_name"] for j in jobs] == [f"{name}-node", f"{name}-vmagent"]
    assert {c["labels"]["instance"] for j in jobs for c in j["static_configs"]} == {name}


def test_setup_metrics_fills_in_the_hostname():
    script = (PI / "metrics" / "setup-metrics.sh").read_text()
    assert 'sed "s/PI_NAME/$name/g" "$SRC/agent.yml"' in script
    # sed rewrites every PI_NAME: a comment that said it would come out wrong on the Pi.
    assert not any("PI_NAME" in line for line in AGENT.read_text().splitlines() if line.startswith("#"))
