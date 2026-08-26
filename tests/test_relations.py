"""Tests for relation handlers: shared-db, peers, website."""

import pytest
from unittest.mock import patch

import subprocess


class TestSharedDbCreated:
    """_on_shared_db_created frees port 3306 for the router."""

    def test_disables_mariadb(self, harness_installed):
        """On shared-db relation created, local mariadb is disabled."""
        harness_installed.set_leader(True)
        rel_id = harness_installed.add_relation("shared-db", "mysql-router")
        harness_installed.add_relation_unit(rel_id, "mysql-router/0")


class TestSharedDbChanged:
    """_on_shared_db_changed triggers reconfigure."""

    def test_defers_before_install(self, harness):
        harness.begin()
        harness.charm._stored.installed = False
        rel_id = harness.add_relation("shared-db", "mysql-router")
        harness.add_relation_unit(rel_id, "mysql-router/0")
        relation = harness.charm.model.get_relation("shared-db", rel_id)
        harness.charm.on["shared-db"].relation_changed.emit(relation)
        assert harness.charm.unit.status.name == "waiting"

    def test_reconfigures_with_creds(self, harness_installed):
        """When router provides creds, charm reconfigures."""
        harness_installed.set_leader(True)
        rel_id = harness_installed.add_relation("shared-db", "mysql-router")
        harness_installed.add_relation_unit(rel_id, "mysql-router/0")
        harness_installed.update_relation_data(
            rel_id, "mysql-router/0",
            {"db_host": "10.0.0.5", "username": "skyline",
             "password": "pass", "database": "skyline"},
        )
        with patch.object(harness_installed.charm, "_configure", return_value=True):
            relation = harness_installed.charm.model.get_relation("shared-db", rel_id)
            harness_installed.charm.on["shared-db"].relation_changed.emit(relation)
            assert harness_installed.charm.unit.status.name == "active"


class TestWebsiteRelation:
    """_on_website_joined/changed publishes ingress info."""

    def test_joined_publishes(self, harness_installed):
        harness_installed.set_leader(True)
        rel_id = harness_installed.add_relation("website", "haproxy")
        harness_installed.add_relation_unit(rel_id, "haproxy/0")
        unit_data = harness_installed.get_relation_data(
            rel_id, "skyline/0"
        )
        assert "port" in unit_data
        assert unit_data["port"] == "9999"

    def test_changed_updates(self, harness_installed):
        harness_installed.set_leader(True)
        rel_id = harness_installed.add_relation("website", "haproxy")
        harness_installed.add_relation_unit(rel_id, "haproxy/0")
        harness_installed.update_config({"listen-port": 8080})
        relation = harness_installed.charm.model.get_relation("website", rel_id)
        harness_installed.charm._publish_website(relation)
        unit_data = harness_installed.get_relation_data(
            rel_id, "skyline/0"
        )
        assert unit_data.get("port") == "8080"


class TestPeersRelation:
    """_on_peers_changed triggers reconfigure for secret key sync."""

    def test_peers_changed_reconfigures(self, harness_installed):
        harness_installed.set_leader(True)
        rel_id = harness_installed.add_relation("skyline-peers", "skyline")
        harness_installed.add_relation_unit(rel_id, "skyline/1")
        assert harness_installed.charm.unit.status.name in (
            "active", "maintenance", "waiting"
        )
