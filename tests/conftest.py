"""Shared fixtures for Skyline charm unit tests."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure src/ is on the path so `import charm` works
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# Ensure tests/ is on the path so `import helpers` works
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest
from ops.testing import Harness

from charm import SkylineCharm
from helpers import VALID_CONFIG


def _noop_run(self, cmd, *args, **kwargs):
    """Replacement for _run that does nothing (no subprocess calls)."""
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def harness(tmp_path):
    """Create a Harness with mock _run and valid config.

    The harness is NOT begun — call harness.begin() or
    harness.begin_with_initial_hooks() as needed in each test.
    """
    h = Harness(SkylineCharm)
    h._backend._path = str(tmp_path)
    h.update_config(VALID_CONFIG)

    # Patch _run so no real subprocess calls happen
    with patch.object(SkylineCharm, "_run", _noop_run):
        yield h


@pytest.fixture
def harness_installed(harness):
    """Harness with charm marked as installed and static_path set."""
    harness.begin()
    harness.charm._stored.installed = True
    harness.charm._stored.static_path = str(
        Path(harness._backend._path) / "static"
    )
    (Path(harness._backend._path) / "static").mkdir(exist_ok=True)
    return harness


@pytest.fixture
def static_dir(harness_installed):
    """The static assets directory for the installed charm."""
    p = Path(harness_installed._backend._path) / "static"
    p.mkdir(exist_ok=True)
    return p
