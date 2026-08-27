"""Tests for JS bundle patching (_patch_container_infra_bundle)."""

from pathlib import Path

import pytest

from helpers import make_bundle_file


class TestPatchContainerInfraBundle:
    """_patch_container_infra_bundle() fixes two upstream Skyline bugs."""

    def test_replaces_bad_pattern(self, harness_installed, static_dir):
        """Single occurrence of the bad pattern is replaced."""
        make_bundle_file(static_dir, content="var x={left:l=0}=r;var y=1;")
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert "{left:l=0}=r||{};" in result
        assert "{left:l=0}=r;" not in result

    def test_replaces_all_occurrences(self, harness_installed, static_dir):
        """Multiple occurrences are ALL replaced, not just the first."""
        content = "a={left:l=0}=r;b={left:l=0}=r;c=1;"
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result.count("{left:l=0}=r||{};") == 2
        assert "{left:l=0}=r;" not in result

    def test_idempotent_v3_marker(self, harness_installed, static_dir):
        """Already-patched file (V3 marker present) is skipped."""
        content = (
            "var x={left:l=0}=r||{};\n"
            'if(e)return"";if(!this.enableCinder)return"";var{newNodes:a}=this.getNodesInput(),{volumes:r}\n'
            "// PATCHED: VOL_QUOTA_PATCHED_V3\n"
        )
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result.count("{left:l=0}=r||{};") == 1

    def test_strips_v1_marker_before_repatching(self, harness_installed, static_dir):
        """V1 marker is removed before re-patching with V3."""
        content = (
            "var x={left:l=0}=r;"
            "\n// PATCHED: VOL_QUOTA_PATCHED_V1\n"
        )
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert "VOL_QUOTA_PATCHED_V1" not in result
        assert "VOL_QUOTA_PATCHED_V3" in result

    def test_strips_v2_marker_before_repatching(self, harness_installed, static_dir):
        """V2 marker is removed before re-patching with V3."""
        content = (
            "var x={left:l=0}=r||{};"
            "\n// PATCHED: VOL_QUOTA_PATCHED_V2\n"
        )
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert "VOL_QUOTA_PATCHED_V2" not in result
        assert "VOL_QUOTA_PATCHED_V3" in result

    def test_removes_gz_companion(self, harness_installed, static_dir):
        """Stale .gz file is deleted after patching the .js."""
        js_path = make_bundle_file(static_dir)
        gz_path = js_path.with_suffix(js_path.suffix + ".gz")
        gz_path.write_bytes(b"fake gzip content")
        assert gz_path.exists()

        harness_installed.charm._patch_container_infra_bundle()
        assert not gz_path.exists()
        assert js_path.exists()

    def test_noop_when_static_path_empty(self, harness):
        """No-op when static_path is not yet discovered."""
        harness.begin()
        harness.charm._stored.static_path = ""
        harness.charm._patch_container_infra_bundle()

    def test_appends_v3_marker(self, harness_installed, static_dir):
        """V3 marker is appended to the patched file."""
        make_bundle_file(static_dir, content="var x={left:l=0}=r;")
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result.strip().endswith("// PATCHED: VOL_QUOTA_PATCHED_V3")

    def test_injects_enablecinder_guard(self, harness_installed, static_dir):
        """V3 patch injects enableCinder guard into checkVolumeQuota."""
        content = (
            'checkVolumeQuota(){var{quotaLoading:e}=this.state;'
            'if(e)return"";var{newNodes:a}=this.getNodesInput(),'
            '{volumes:r}=(0,S.toJS)(this.projectStore.cinderQuota)||{},'
            '{left:l=0}=r||{};return-1!==l&&l<a?this.getQuotaMessage(a,r,t("Volume")):""}'
        )
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert 'if(!this.enableCinder)return""' in result

    def test_enablecinder_guard_idempotent(self, harness_installed, static_dir):
        """Already-patched file with enableCinder guard is not double-patched."""
        content = (
            'checkVolumeQuota(){var{quotaLoading:e}=this.state;'
            'if(e)return"";if(!this.enableCinder)return"";var{newNodes:a}=this.getNodesInput(),'
            '{volumes:r}=(0,S.toJS)(this.projectStore.cinderQuota)||{},'
            '{left:l=0}=r||{};return-1!==l&&l<a?this.getQuotaMessage(a,r,t("Volume")):""}'
            "\n// PATCHED: VOL_QUOTA_PATCHED_V3\n"
        )
        make_bundle_file(static_dir, content=content)
        harness_installed.charm._patch_container_infra_bundle()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert result.count('if(!this.enableCinder)return""') == 1
