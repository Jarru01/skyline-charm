"""Tests for JS bundle patching (_patch_container_infra_bundle)."""

from pathlib import Path

import pytest

from helpers import make_bundle_file


class TestPatchContainerInfraBundle:
    """_patch_container_infra_bundle() fixes the checkVolumeQuota TypeError."""

    def test_replaces_bad_pattern(self, harness_installed, static_dir):
        """Single occurrence of the bad pattern is replaced."""
        make_bundle_file(static_dir, content="var x={left:l=0}=r;var y=1;")
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert "{left:l=0}=r||{};" in result
        assert "{left:l=0}=r;" not in result  # Original gone

    def test_replaces_all_occurrences(self, harness_installed, static_dir):
        """Multiple occurrences are ALL replaced, not just the first."""
        content = "a={left:l=0}=r;b={left:l=0}=r;c=1;"
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result.count("{left:l=0}=r||{};") == 2
        assert "{left:l=0}=r;" not in result

    def test_idempotent_v2_marker(self, harness_installed, static_dir):
        """Already-patched file (V2 marker present) is skipped."""
        content = "var x={left:l=0}=r||{};\n// PATCHED: VOL_QUOTA_PATCHED_V2\n"
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result.count("{left:l=0}=r||{};") == 1  # Not doubled

    def test_strips_v1_marker_before_repatching(self, harness_installed, static_dir):
        """V1 marker is removed before re-patching with V2."""
        content = (
            "var x={left:l=0}=r;"
            "\n// PATCHED: VOL_QUOTA_PATCHED_V1\n"
        )
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert "VOL_QUOTA_PATCHED_V1" not in result
        assert "VOL_QUOTA_PATCHED_V2" in result

    def test_removes_gz_companion(self, harness_installed, static_dir):
        """Stale .gz file is deleted after patching the .js."""
        js_path = make_bundle_file(static_dir)
        gz_path = js_path.with_suffix(js_path.suffix + ".gz")
        gz_path.write_bytes(b"fake gzip content")
        assert gz_path.exists()

        harness_installed.charm._patch_container_infra_bundle()
        assert not gz_path.exists()
        assert js_path.exists()

    def test_skips_files_without_target(self, harness_installed, static_dir):
        """Files without the bad pattern are left untouched."""
        make_bundle_file(static_dir, content="var x=1;var y=2;")
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result == "var x=1;var y=2;"

    def test_noop_when_static_path_empty(self, harness):
        """No-op when static_path is not yet discovered."""
        harness.begin()
        harness.charm._stored.static_path = ""
        # Should not raise
        harness.charm._patch_container_infra_bundle()

    def test_appends_v2_marker(self, harness_installed, static_dir):
        """V2 marker is appended to the patched file."""
        make_bundle_file(static_dir, content="var x={left:l=0}=r;")
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result.strip().endswith("// PATCHED: VOL_QUOTA_PATCHED_V2")
