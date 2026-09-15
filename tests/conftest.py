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

import charm as charm_module
from charm import SkylineCharm
from helpers import VALID_CONFIG


def _noop_run(self, cmd, *args, **kwargs):
    """Replacement for _run that does nothing (no subprocess calls)."""
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def isolated_fs(tmp_path, monkeypatch):
    """Keep every test hermetic: no test may touch real /etc or /opt paths."""
    root = tmp_path / "fs"
    conf = root / "etc-skyline"
    monkeypatch.setattr(charm_module, "VENV_DIR", root / "venv")
    monkeypatch.setattr(charm_module, "VENV_BIN", root / "venv" / "bin")
    monkeypatch.setattr(charm_module, "VENV_PY", root / "venv" / "bin" / "python3")
    monkeypatch.setattr(charm_module, "VENV_PIP", root / "venv" / "bin" / "pip")
    monkeypatch.setattr(charm_module, "APISERVER_SRC", root / "apiserver-src")
    monkeypatch.setattr(charm_module, "SKYLINE_CONF_DIR", conf)
    monkeypatch.setattr(charm_module, "SKYLINE_LOG_DIR", root / "log-skyline")
    monkeypatch.setattr(charm_module, "SKYLINE_POLICY_DIR", conf / "policy")
    monkeypatch.setattr(charm_module, "SYSTEMD_UNIT_PATH", root / "systemd" / "skyline.service")
    monkeypatch.setattr(charm_module, "NGINX_CONF_PATH", root / "nginx" / "nginx.conf")
    monkeypatch.setattr(charm_module, "GUNICORN_CONF_PATH", conf / "gunicorn.py")
    monkeypatch.setattr(charm_module, "SKYLINE_YAML_PATH", conf / "skyline.yaml")
    monkeypatch.setattr(charm_module, "GENERATED_NGINX_PATH", conf / "nginx.conf.generated")
    monkeypatch.setattr(charm_module, "CERTS_DIR", conf / "certs")
    monkeypatch.setattr(charm_module, "TLS_CERT_PATH", conf / "certs" / "server.crt")
    monkeypatch.setattr(charm_module, "TLS_KEY_PATH", conf / "certs" / "server.key")
    monkeypatch.setattr(charm_module, "TLS_CA_PATH", conf / "certs" / "vault-ca.crt")
    monkeypatch.setattr(charm_module, "SYSTEM_CA_PATH", root / "ca" / "skyline-vault-ca.crt")
    monkeypatch.setattr(charm_module, "MARIADB_CNF_PATH", root / "mysql" / "60-skyline.cnf")


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
