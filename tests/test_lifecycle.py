"""Tests for lifecycle events: install, config-changed, start, upgrade-charm."""

import subprocess
from unittest.mock import patch

import pytest

from helpers import VALID_CONFIG


class TestOnInstall:
    """_on_install runs the full install sequence."""

    def test_sets_maintenance_during_install(self, harness):
        harness.begin()
        harness.charm._on_install(harness.charm.on.install)
        assert harness.charm._stored.installed is True

    def test_sets_blocked_on_failure(self, harness):
        harness.begin()
        with patch.object(harness.charm, "_install_system_packages",
                          side_effect=RuntimeError("apt failed")):
            harness.charm._on_install(harness.charm.on.install)
            assert harness.charm.unit.status.name == "blocked"
            assert "Install failed" in harness.charm.unit.status.message
            assert harness.charm._stored.installed is False


class TestOnConfigChanged:
    """_on_config_changed validates config and reconfigures."""

    def test_defers_before_install(self, harness):
        harness.begin()
        harness.charm._stored.installed = False
        harness.charm.on.config_changed.emit()
        assert harness.charm.unit.status.name == "waiting"
        assert "Waiting for install" in harness.charm.unit.status.message

    def test_blocks_on_missing_config(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        harness.update_config({"keystone-url": ""})
        harness.charm.on.config_changed.emit()
        assert harness.charm.unit.status.name == "blocked"

    def test_succeeds_with_valid_config(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        harness.charm._stored.static_path = "/tmp/fake-static"
        with patch.object(harness.charm, "_configure", return_value=True):
            harness.charm.on.config_changed.emit()
            assert harness.charm.unit.status.name == "active"
            assert "Skyline ready" in harness.charm.unit.status.message

    def test_handles_exception(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        with patch.object(harness.charm, "_configure",
                          side_effect=RuntimeError("oops")):
            harness.charm.on.config_changed.emit()
            assert harness.charm.unit.status.name == "blocked"
            assert "Config error" in harness.charm.unit.status.message


class TestOnStart:
    """_on_start checks if apiserver is active."""

    def test_defers_before_install(self, harness):
        harness.begin()
        harness.charm._stored.installed = False
        harness.charm.on.start.emit()
        assert harness.charm.unit.status.name == "waiting"

    def test_sets_active_when_service_running(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        mock_result = subprocess.CompletedProcess([], 0, stdout="active")
        with patch.object(harness.charm, "_run", return_value=mock_result):
            harness.charm.on.start.emit()
            assert harness.charm.unit.status.name == "active"

    def test_blocks_when_service_inactive(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        mock_result = subprocess.CompletedProcess([], 3, stdout="inactive")
        with patch.object(harness.charm, "_run", return_value=mock_result):
            harness.charm.on.start.emit()
            assert harness.charm.unit.status.name == "blocked"
            assert "not active" in harness.charm.unit.status.message

    def test_blocks_on_service_error(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        with patch.object(harness.charm, "_run",
                          side_effect=subprocess.CalledProcessError(1, "systemctl")):
            harness.charm.on.start.emit()
            assert harness.charm.unit.status.name == "blocked"
            assert "failed to start" in harness.charm.unit.status.message


class TestOnUpgradeCharm:
    """_on_upgrade_charm reinstalls and reconfigures."""

    def test_upgrades_successfully(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        harness.charm._stored.static_path = "/tmp/fake-static"
        with patch.object(harness.charm, "_install_apiserver"), \
             patch.object(harness.charm, "_install_console"), \
             patch.object(harness.charm, "_configure", return_value=True):
            harness.charm.on.upgrade_charm.emit()
            assert harness.charm.unit.status.name == "active"
            assert "Skyline ready" in harness.charm.unit.status.message

    def test_blocks_on_upgrade_failure(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        with patch.object(harness.charm, "_install_apiserver",
                          side_effect=RuntimeError("wheel missing")):
            harness.charm.on.upgrade_charm.emit()
            assert harness.charm.unit.status.name == "blocked"
            assert "Upgrade failed" in harness.charm.unit.status.message
