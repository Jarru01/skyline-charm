"""Tests for pure helper functions — no mocking needed."""

import tarfile
import io
from pathlib import Path

import pytest

from charm import SkylineCharm


class TestKeystoneUrl:
    """_keystone_url() normalises the keystone URL."""

    def test_appends_v3_when_missing(self, harness):
        harness.begin()
        harness.update_config({"keystone-url": "https://keystone.example.com:5000"})
        url = harness.charm._keystone_url()
        assert url.endswith("/v3/")

    def test_preserves_existing_v3(self, harness):
        harness.begin()
        harness.update_config({"keystone-url": "https://keystone.example.com:5000/v3"})
        url = harness.charm._keystone_url()
        assert url == "https://keystone.example.com:5000/v3/"

    def test_strips_trailing_slash_before_v3(self, harness):
        harness.begin()
        harness.update_config({"keystone-url": "https://keystone.example.com:5000/v3/"})
        url = harness.charm._keystone_url()
        assert url == "https://keystone.example.com:5000/v3/"

    def test_handles_bare_host_port(self, harness):
        harness.begin()
        harness.update_config({"keystone-url": "https://10.0.0.1:5000"})
        url = harness.charm._keystone_url()
        assert url == "https://10.0.0.1:5000/v3/"


class TestMissingRequiredConfig:
    """_missing_required_config() validates required fields."""

    def test_empty_config_blocks(self, harness):
        harness.begin()
        harness.update_config({"keystone-url": "", "system-user-password": ""})
        error = harness.charm._missing_required_config()
        assert "keystone-url" in error

    def test_missing_password_only(self, harness):
        harness.begin()
        harness.update_config({"keystone-url": "https://k:5000/v3/", "system-user-password": ""})
        error = harness.charm._missing_required_config()
        assert "system-user-password" in error
        assert "keystone-url" not in error

    def test_both_set_returns_empty(self, harness):
        harness.begin()
        error = harness.charm._missing_required_config()
        assert error == ""


class TestDatabaseUrl:
    """_database_url() picks the right database source."""

    def test_from_shared_db_data(self, harness):
        harness.begin()
        rel_id = harness.add_relation("shared-db", "mysql-router")
        harness.add_relation_unit(rel_id, "mysql-router/0")
        harness.update_relation_data(
            rel_id, "mysql-router/0",
            {"db_host": "10.0.0.5", "db_port": "3306",
             "username": "skyline", "password": "pass123",
             "database": "skyline"},
        )
        url = harness.charm._database_url()
        assert "10.0.0.5" in url
        assert "skyline" in url
        assert "pass123" in url

    def test_from_config_when_no_relation(self, harness):
        harness.begin()
        harness.update_config({"database-url": "mysql://u:p@db-host:3306/sk"})
        url = harness.charm._database_url()
        assert url == "mysql://u:p@db-host:3306/sk"

    def test_local_db_localhost_13306(self, harness):
        harness.begin()
        url = harness.charm._database_url()
        assert "localhost:13306" in url
        assert "mysql://skyline:" in url


class TestSafeExtract:
    """_safe_extract() rejects path-traversal tarballs."""

    def test_rejects_path_traversal(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="../escape.txt")
            info.size = 0
            tar.addfile(info)
        buf.seek(0)
        with pytest.raises(RuntimeError, match="Refusing to extract"):
            with tarfile.open(fileobj=buf, mode="r") as tar:
                SkylineCharm._safe_extract(tar, tmp_path)

    def test_accepts_normal_paths(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="subdir/file.txt")
            data = b"hello"
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        with tarfile.open(fileobj=buf, mode="r") as tar:
            SkylineCharm._safe_extract(tar, tmp_path)
        assert (tmp_path / "subdir" / "file.txt").read_bytes() == b"hello"
