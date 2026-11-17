"""Shared test helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

PI = Path(__file__).resolve().parent.parent / "pi"
# The handheld's scripts import handheld_common from beside them, as installed.
sys.path.insert(0, str(PI))
sys.path.insert(0, str(PI / "eink"))  # eink-status imports eink_art from beside it


def load_script(name: str, path: Path) -> ModuleType:
    """A script under pi/ as a module, loaded by path (their names have
    dashes). Registered in sys.modules first: dataclasses look it up."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
