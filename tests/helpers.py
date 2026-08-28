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


def make_main_bundle_file(static_dir, filename="main.bundle.1786807402.js",
                          content=None):
    """Create a fake main bundle JS file with MagnumClient pattern."""
    if content is None:
        content = (
            'class l extends o.default{get baseUrl(){return(0,i.magnumBase)()}'
            "get resources(){return["
            '{name:"clusters",key:"clusters",responseKey:"cluster",'
            "extendOperations:["
            '{name:"resize",key:"actions/resize",method:"post"},'
            '{name:"upgrade",key:"actions/upgrade",method:"post"}'
            "]}"
            "]}}"
        )
    path = static_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def make_apiserver_venv(tmp_path, init_content=None):
    """Create a fake skyline_apiserver package tree and return its venv_lib.

    The layout mirrors the production install:
    <venv>/lib/python3.10/site-packages/skyline_apiserver/api/v1/__init__.py
    """
    v1_dir = (
        tmp_path / "venv" / "lib" / "python3.10" / "site-packages"
        / "skyline_apiserver" / "api" / "v1"
    )
    v1_dir.mkdir(parents=True, exist_ok=True)
    if init_content is None:
        init_content = (
            "from skyline_apiserver.api.v1 import "
            "contrib, extension, login, policy, prometheus, setting\n"
            "\n"
            "api_router = APIRouter()\n"
            'api_router.include_router(setting.router, tags=["Setting"])\n'
        )
    (v1_dir / "__init__.py").write_text(init_content, encoding="utf-8")
    return tmp_path / "venv" / "lib"
