"""Shared pytest configuration.

The engine/simulator/replay tests are plain Python.  The Home Assistant tests
in ``tests/ha`` use pytest-homeassistant-custom-component; its plugin is only
required there (see ``tests/ha/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PARENT = ROOT / "custom_components" / "adaptive_thermostat"
for path in (ROOT, ENGINE_PARENT, ROOT / "tests" / "sim", ROOT / "tools" / "replay"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
