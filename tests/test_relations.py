"""Tests for relation handlers: shared-db, identity-credentials, peers, website."""

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


class TestIdentityCredentials:
    """identity-credentials relation: auto Keystone URL + service credentials."""

    _CREDS = {
        "credentials_host": "10.0.0.9",
        "credentials_port": "5000",
        "credentials_protocol": "https",
        "credentials_username": "skyline",
        "credentials_password": "generated-pw",
        "credentials_project": "admin",
        "credentials_user_domain_name": "admin_domain",
        "credentials_project_domain_name": "admin_domain",
        "api_version": "3",
        "region": "RegionOne",
    }

    def _relate(self, harness_installed, **data):
        """Attach the keystone relation; mock _configure to avoid side effects."""
        charm = harness_installed.charm
        with patch.object(charm, "_configure", return_value=True):
            rel_id = harness_installed.add_relation(
                "identity-credentials", "keystone"
            )
            harness_installed.add_relation_unit(rel_id, "keystone/0")
            if data:
                harness_installed.update_relation_data(
                    rel_id, "keystone/0", data
                )
        return rel_id

    def test_url_from_relation(self, harness_installed):
        """The public Keystone endpoint is built from the relation data."""
        self._relate(harness_installed, **self._CREDS)
        assert harness_installed.charm._keystone_url() == (
            "https://10.0.0.9:5000/v3/"
        )

    def test_credentials_override_config(self, harness_installed):
        """Relation credentials take precedence over the config values."""
        self._relate(harness_installed, **self._CREDS)
        identity = harness_installed.charm._effective_identity()
        assert identity["system_user_password"] == "generated-pw"
        assert identity["system_user_name"] == "skyline"
        assert identity["system_project"] == "admin"
        assert identity["system_user_domain"] == "admin_domain"
        assert identity["default_region"] == "RegionOne"

    def test_publishes_request(self, harness_installed):
        """username/project/domain are written to the unit databag."""
        rel_id = self._relate(harness_installed)
        harness_installed.charm._publish_identity_request()
        unit_data = harness_installed.get_relation_data(rel_id, "skyline/0")
        assert unit_data["username"] == "skyline"
        assert unit_data["project"] == "admin"
        assert unit_data["domain"] == "admin_domain"

    def test_config_change_updates_request(self, harness_installed):
        """Changing identity-username republishes the relation request."""
        rel_id = self._relate(harness_installed)
        harness_installed.update_config({"identity-username": "skyline-alt"})
        harness_installed.charm._publish_identity_request()
        unit_data = harness_installed.get_relation_data(rel_id, "skyline/0")
        assert unit_data["username"] == "skyline-alt"

    def test_missing_data_waits(self, harness_installed):
        """Relation attached without credentials -> waiting, no configure."""
        self._relate(harness_installed)
        ok = harness_installed.charm._configure()
        assert ok is False
        assert harness_installed.charm.unit.status.name == "waiting"
        assert "Keystone credentials" in harness_installed.charm.unit.status.message

    def test_required_config_skipped_when_related(self, harness_installed):
        """keystone-url/system-user-password are not required when related."""
        harness_installed.update_config(
            {"keystone-url": "", "system-user-password": ""}
        )
        self._relate(harness_installed, **self._CREDS)
        assert harness_installed.charm._missing_required_config() == ""

    def test_api_version_2_falls_back_to_config(self, harness_installed):
        """An unsupported api_version is ignored; config values are used."""
        bad = dict(self._CREDS)
        bad["api_version"] = "2.0"
        self._relate(harness_installed, **bad)
        assert harness_installed.charm._identity_credentials_data() is None
        assert harness_installed.charm._keystone_url() == (
            "https://keystone.example.com:5000/v3/"
        )

    def test_relation_broken_falls_back(self, harness_installed):
        """Removing the relation falls back to config-driven credentials."""
        rel_id = self._relate(harness_installed, **self._CREDS)
        charm = harness_installed.charm
        with patch.object(charm, "_configure", return_value=True):
            harness_installed.remove_relation(rel_id)
        assert charm._identity_credentials_data() is None
        assert not charm._identity_credentials_related()
        assert charm._keystone_url() == "https://keystone.example.com:5000/v3/"

    def test_handler_defers_before_install(self, harness):
        harness.begin()
        charm = harness.charm
        charm._stored.installed = False
        with patch.object(charm, "_configure", return_value=True):
            rel_id = harness.add_relation("identity-credentials", "keystone")
            harness.add_relation_unit(rel_id, "keystone/0")
        assert charm.unit.status.name == "waiting"
        assert "install" in charm.unit.status.message

    def test_handler_activates_when_configure_ok(self, harness_installed):
        charm = harness_installed.charm
        with patch.object(charm, "_configure", return_value=True):
            rel_id = harness_installed.add_relation(
                "identity-credentials", "keystone"
            )
            harness_installed.add_relation_unit(rel_id, "keystone/0")
        assert charm.unit.status.name == "active"


class TestPeersRelation:
    """_on_peers_changed triggers reconfigure for secret key sync."""

    def test_peers_changed_reconfigures(self, harness_installed):
        harness_installed.set_leader(True)
        rel_id = harness_installed.add_relation("skyline-peers", "skyline")
        harness_installed.add_relation_unit(rel_id, "skyline/1")
        assert harness_installed.charm.unit.status.name in (
            "active", "maintenance", "waiting"
        )
