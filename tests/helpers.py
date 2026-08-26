"""Shared helper functions for tests — importable as a regular module."""

import subprocess
import textwrap
from pathlib import Path

from charm import SkylineCharm


# ── Default config for a valid charm setup ──────────────────────────────────

VALID_CONFIG = {
    "keystone-url": "https://keystone.example.com:5000/v3/",
    "system-user-password": "test-password-123",
    "database-url": "",
    "database-password": "",
    "default-region": "RegionOne",
    "system-user-name": "skyline",
    "system-user-domain": "admin_domain",
    "system-project": "admin",
    "system-project-domain": "admin_domain",
    "interface-type": "public",
    "listen-port": 9999,
    "ssl-enabled": False,
    "debug": False,
    "secret-key": "",
    "prometheus-endpoint": "",
    "prometheus-enable-basic-auth": False,
    "prometheus-basic-auth-user": "",
    "prometheus-basic-auth-password": "",
    "sso-enabled": False,
    "sso-region": "RegionOne",
    "enforce-new-defaults": False,
    "reclaim-instance-interval": 604800,
    "gunicorn-workers": 0,
    "gunicorn-timeout": 300,
}


def noop_run(self, cmd, *args, **kwargs):
    """Replacement for _run that does nothing (no subprocess calls)."""
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def make_bundle_file(static_dir, filename="container-infra.bundle.1786807402.js",
                     content=None):
    """Create a fake container-infra bundle JS file."""
    if content is None:
        content = (
            "var x={left:l=0}=r;"
            "function checkVolumeQuota(){var cinderQuota;{left:l=0}=cinderQuota}"
        )
    path = static_dir / filename
    path.write_text(content, encoding="utf-8")
    return path
