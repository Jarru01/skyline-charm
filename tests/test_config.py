"""Tests for config validation, DB mode logic, and secret key handling."""

from unittest.mock import patch

import pytest

from charm import SkylineCharm, LOCAL_MARIADB_PORT


class TestConfigValidation:
    """config-changed with missing required config → BlockedStatus."""

    def test_blocks_on_missing_keystone_url(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        harness.update_config({"keystone-url": ""})
        harness.charm.on.config_changed.emit()
        assert harness.charm.unit.status.name == "blocked"
        assert "keystone-url" in harness.charm.unit.status.message

    def test_blocks_on_missing_password(self, harness):
        harness.begin()
        harness.charm._stored.installed = True
        harness.update_config({"keystone-url": "https://k:5000/v3/", "system-user-password": ""})
        harness.charm.on.config_changed.emit()
        assert harness.charm.unit.status.name == "blocked"
        assert "system-user-password" in harness.charm.unit.status.message


class TestUsingLocalDb:
    """_using_local_db() determines the database backend."""

    def test_local_by_default(self, harness):
        harness.begin()
        assert harness.charm._using_local_db() is True

    def test_config_url_skips_local(self, harness):
        harness.begin()
        harness.update_config({"database-url": "mysql://u:p@h:3306/sk"})
        assert harness.charm._using_local_db() is False

    def test_shared_db_takes_precedence(self, harness):
        harness.begin()
        rel_id = harness.add_relation("shared-db", "mysql-router")
        harness.add_relation_unit(rel_id, "mysql-router/0")
        harness.update_relation_data(
            rel_id, "mysql-router/0",
            {"db_host": "10.0.0.5", "username": "skyline",
             "password": "pass", "database": "skyline"},
        )
        assert harness.charm._using_local_db() is False

    def test_shared_db_related_no_data(self, harness):
        """Router attached but no credentials yet → not local (waiting)."""
        harness.begin()
        rel_id = harness.add_relation("shared-db", "mysql-router")
        harness.add_relation_unit(rel_id, "mysql-router/0")
        assert harness.charm._using_local_db() is False


class TestSharedDbData:
    """_shared_db_data() parses router relation data."""

    def test_parses_mysql_router_keys(self, harness):
        harness.begin()
        rel_id = harness.add_relation("shared-db", "mysql-router")
        harness.add_relation_unit(rel_id, "mysql-router/0")
        harness.update_relation_data(
            rel_id, "mysql-router/0",
            {"db_host": "10.0.0.5", "db_port": "3307",
             "username": "sky", "password": "pw", "database": "db"},
        )
        data = harness.charm._shared_db_data()
        assert data is not None
        assert data["host"] == "10.0.0.5"
        assert data["port"] == "3307"
        assert data["username"] == "sky"
        assert data["password"] == "pw"
        assert data["database"] == "db"

    def test_parses_legacy_keys(self, harness):
        harness.begin()
        rel_id = harness.add_relation("shared-db", "mysql-router")
        harness.add_relation_unit(rel_id, "mysql-router/0")
        harness.update_relation_data(
            rel_id, "mysql-router/0",
            {"host": "10.0.0.6", "port": "3306",
             "user": "skyline", "db_name": "skyline"},
        )
        data = harness.charm._shared_db_data()
        assert data is not None
        assert data["host"] == "10.0.0.6"
        assert data["username"] == "skyline"
        assert data["database"] == "skyline"

    def test_returns_none_without_data(self, harness):
        harness.begin()
        assert harness.charm._shared_db_data() is None


class TestDbPassword:
    """_db_password() generates or retrieves the local DB password."""

    def test_auto_generates_when_empty(self, harness):
        harness.begin()
        pw = harness.charm._db_password()
        assert len(pw) > 10
        assert harness.charm._stored.db_password == pw

    def test_uses_config_value(self, harness):
        harness.begin()
        harness.update_config({"database-password": "my-secret"})
        pw = harness.charm._db_password()
        assert pw == "my-secret"

    def test_returns_stored_on_subsequent_calls(self, harness):
        harness.begin()
        pw1 = harness.charm._db_password()
        pw2 = harness.charm._db_password()
        assert pw1 == pw2


class TestSecretKey:
    """_secret_key() follows config > peers > stored precedence."""

    def test_config_wins(self, harness):
        harness.begin()
        harness.update_config({"secret-key": "explicit-key"})
        assert harness.charm._secret_key() == "explicit-key"

    def test_generates_stored_key(self, harness):
        harness.begin()
        key = harness.charm._secret_key()
        assert len(key) > 10
        assert harness.charm._stored.secret_key == key

    def test_consistent_across_calls(self, harness):
        harness.begin()
        k1 = harness.charm._secret_key()
        k2 = harness.charm._secret_key()
        assert k1 == k2


class TestPublishSecretKey:
    """_publish_secret_key() writes to peers app databag (leader only)."""

    def test_leader_publishes(self, harness):
        harness.begin()
        harness.set_leader(True)
        rel_id = harness.add_relation("skyline-peers", "skyline")
        harness.add_relation_unit(rel_id, "skyline/1")
        harness.charm._publish_secret_key()
        app_data = harness.get_relation_data(rel_id, "skyline")
        assert "secret_key" in app_data
        assert len(app_data["secret_key"]) > 10

    def test_non_leader_does_not_publish(self, harness):
        harness.begin()
        harness.set_leader(False)
        rel_id = harness.add_relation("skyline-peers", "skyline")
        harness.add_relation_unit(rel_id, "skyline/1")
        harness.charm._publish_secret_key()
        app_data = harness.get_relation_data(rel_id, "skyline")
        assert "secret_key" not in app_data

    def test_config_value_published(self, harness):
        harness.begin()
        harness.set_leader(True)
        harness.update_config({"secret-key": "my-rotation-key"})
        rel_id = harness.add_relation("skyline-peers", "skyline")
        harness.add_relation_unit(rel_id, "skyline/1")
        harness.charm._publish_secret_key()
        app_data = harness.get_relation_data(rel_id, "skyline")
        assert app_data["secret_key"] == "my-rotation-key"
