"""pi/bootstrap.sh: setup steps that depend on each other run in order."""

import re
import unittest
from pathlib import Path

PI = Path(__file__).resolve().parent.parent / "pi"


def setup_steps() -> list[str]:
    """The setup scripts bootstrap.sh runs, in order."""
    return re.findall(r'bash "\$SRC/([\w/.-]+\.sh)"', (PI / "bootstrap.sh").read_text())


class BootstrapOrderTest(unittest.TestCase):
    def test_kismet_login_before_the_units_that_load_it(self):
        # pi-metrics, eink-status and status-leds load kismet_httpd.conf with
        # LoadCredential; started before it exists, they fail and stop the run.
        steps = setup_steps()
        login = steps.index("setup-kismet-login.sh")
        for later in ("metrics/setup-metrics.sh", "eink/setup-eink.sh", "leds/setup-leds.sh"):
            if later in steps:
                self.assertLess(login, steps.index(later), later)

    def test_every_unit_with_the_kismet_credential_is_covered(self):
        units = {p.relative_to(PI).parts[0] for p in PI.rglob("*.service")
                 if "kismet_httpd.conf" in p.read_text()}
        self.assertLessEqual(units, {"metrics", "eink", "leds"})


if __name__ == "__main__":
    unittest.main()
