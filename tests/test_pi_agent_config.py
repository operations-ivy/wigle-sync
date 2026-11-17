"""pi/metrics/agent.yml: vmagent's scrape config, named for each Pi by
setup-metrics.sh."""

from __future__ import annotations

import subprocess

import yaml

from tests.conftest import PI

AGENT = PI / "metrics" / "agent.yml"


def rendered(name: str) -> dict:
    """agent.yml as setup-metrics.sh writes it on a Pi called `name`."""
    out = subprocess.run(["sed", f"s/PI_NAME/{name}/g", str(AGENT)], capture_output=True, text=True, check=True)
    return yaml.safe_load(out.stdout)


def test_each_pi_is_its_own_instance():
    for name in ("brick69", "scootypuffsr"):
        jobs = rendered(name)["scrape_configs"]
        assert [j["job_name"] for j in jobs] == [f"{name}-node", f"{name}-vmagent"]
        assert {c["labels"]["instance"] for j in jobs for c in j["static_configs"]} == {name}


def test_setup_metrics_fills_in_the_hostname():
    script = (PI / "metrics" / "setup-metrics.sh").read_text()
    assert 'sed "s/PI_NAME/$name/g" "$SRC/agent.yml"' in script
    assert "PI_NAME" not in "".join(line for line in AGENT.read_text().splitlines(True) if line.startswith("#"))
