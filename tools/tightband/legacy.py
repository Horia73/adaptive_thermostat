"""Load a released engine version next to the current one.

``load_engine(ref)`` extracts ``custom_components/adaptive_thermostat/engine``
at a git ref into a temporary package named ``engine_<ref>`` and returns the
namespace the simulator drives (``ZoneCore``, ``ZoneConfig``, ``BoilerPolicy``,
``ZoneReport``).  The engine only uses relative imports, so two versions can
live in one process.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
ENGINE_PATH = "custom_components/adaptive_thermostat/engine"
_CACHE: dict[str, SimpleNamespace] = {}


def load_engine(ref: str) -> SimpleNamespace:
    """Return the engine API at git ``ref`` (e.g. ``v2.1.0`` or a commit)."""
    if ref in _CACHE:
        return _CACHE[ref]
    name = "engine_" + re.sub(r"\W", "_", ref)
    root = Path(tempfile.mkdtemp(prefix="at_engine_"))
    target = root / name
    target.mkdir()
    files = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "--name-only", f"{ref}:{ENGINE_PATH}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    for fname in files:
        if not fname.endswith(".py"):
            continue
        src = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{ref}:{ENGINE_PATH}/{fname}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        (target / fname).write_text(src)
    sys.path.insert(0, str(root))
    zone = importlib.import_module(f"{name}.zone")
    boiler = importlib.import_module(f"{name}.boiler")
    controller = importlib.import_module(f"{name}.controller")
    estimator = importlib.import_module(f"{name}.estimator")
    ns = SimpleNamespace(
        ZoneCore=zone.ZoneCore,
        ZoneConfig=zone.ZoneConfig,
        BoilerPolicy=boiler.BoilerPolicy,
        BoilerConfig=boiler.BoilerConfig,
        ZoneReport=boiler.ZoneReport,
        ControllerConfig=controller.ControllerConfig,
        ZoneEstimator=estimator.ZoneEstimator,
        module=zone,
        ref=ref,
    )
    _CACHE[ref] = ns
    return ns
