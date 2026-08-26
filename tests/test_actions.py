"""Tests for all 6 action handlers."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ops._private.harness import ActionFailed

from charm import SKYLINE_YAML_PATH
from helpers import make_bundle_file


class TestActionGetStaticPath:
    """get-static-path action returns the stored path."""

    def test_returns_stored_path(self, harness_installed):
        out = harness_installed.run_action("get-static-path")
        assert out.results["static-path"] == harness_installed.charm._stored.static_path

    def test_returns_placeholder_when_empty(self, harness):
        harness.begin()
        out = harness.run_action("get-static-path")
        assert out.results["static-path"] == "(not yet discovered)"


class TestActionShowConfig:
    """show-config action reads skyline.yaml."""

    def test_returns_rendered_yaml(self, harness_installed, tmp_path):
        fake_yaml = tmp_path / "skyline.yaml"
        fake_yaml.write_text("database_url: mysql://test\n")
        with patch("charm.SKYLINE_YAML_PATH", fake_yaml):
            out = harness_installed.run_action("show-config")
            assert "mysql://test" in out.results["skyline-yaml"]

    def test_returns_placeholder_when_missing(self, harness_installed, tmp_path):
        fake_yaml = tmp_path / "skyline.yaml"
        with patch("charm.SKYLINE_YAML_PATH", fake_yaml):
            out = harness_installed.run_action("show-config")
            assert out.results["skyline-yaml"] == "(not yet rendered)"


class TestActionPatchFrontend:
    """patch-frontend action patches JS bundles."""

    def test_patches_bundle(self, harness_installed, static_dir):
        make_bundle_file(static_dir)
        out = harness_installed.run_action("patch-frontend")
        assert out.results["patched"] == 1
        assert out.results["skipped"] == 0

    def test_skips_already_patched(self, harness_installed, static_dir):
        content = "var x={left:l=0}=r||{};\n// PATCHED: VOL_QUOTA_PATCHED_V2\n"
        make_bundle_file(static_dir, content=content)
        out = harness_installed.run_action("patch-frontend")
        assert out.results["patched"] == 0
        assert out.results["skipped"] == 1

    def test_fails_when_not_installed(self, harness):
        harness.begin()
        harness.charm._stored.installed = False
        with pytest.raises(ActionFailed):
            harness.run_action("patch-frontend")


class TestActionRegenerateNginx:
    """regenerate-nginx action regenerates nginx config."""

    def test_fails_when_not_installed(self, harness):
        harness.begin()
        harness.charm._stored.installed = False
        with pytest.raises(ActionFailed):
            harness.run_action("regenerate-nginx")

    def test_reports_source(self, harness_installed):
        with patch.object(harness_installed.charm, "_generate_nginx_config",
                          return_value=True):
            out = harness_installed.run_action("regenerate-nginx")
            assert out.results["source"] == "keystone-catalog"


class TestActionRestartServices:
    """restart-services action restarts services."""

    def test_restarts(self, harness_installed):
        out = harness_installed.run_action("restart-services")
        assert out.results["result"] == "Services restarted/reloaded"


class TestActionDbSync:
    """db-sync action runs database migration."""

    def test_runs_db_sync(self, harness_installed):
        out = harness_installed.run_action("db-sync")
        assert out.results["result"] == "db_sync completed successfully"

    def test_reports_failure(self, harness_installed):
        with patch.object(harness_installed.charm, "_run_db_sync",
                          side_effect=RuntimeError("migration failed")):
            with pytest.raises(ActionFailed):
                harness_installed.run_action("db-sync")
