"""Tests for JS bundle patching (container-infra, main, kubeconfig endpoint)."""

from pathlib import Path

import pytest

from charm import SkylineCharm, _KUBECONFIG_ENDPOINT_SRC
from helpers import make_apiserver_venv, make_bundle_file, make_main_bundle_file


# ── Realistic fake bundle content for kubeconfig tests ────────────────────────

_FAKE_MAIN_BUNDLE = (
    'class l extends o.default{get baseUrl(){return(0,i.magnumBase)()}'
    "get resources(){return["
    '{name:"clusters",key:"clusters",responseKey:"cluster",'
    "extendOperations:["
    '{name:"resize",key:"actions/resize",method:"post"},'
    '{name:"upgrade",key:"actions/upgrade",method:"post"}'
    "]}"
    "]}}"
)

# Matches actual webpack module 1696 from deployed bundle
_FAKE_CI_BUNDLE = (
    '1696:function(e,t,a){"use strict";var r=a(20),l=a(21);'
    'r(t,"__esModule",{value:!0}),t.default=void 0;'
    "var n=l(a(4307)),i=l(a(4308)),o=l(a(1488)),s={"
    "actionConfigs:{rowActions:{firstAction:n.default,moreActions:[{action:i.default}]},"
    "batchActions:[n.default],primaryActions:[o.default]},"
    "actionConfigsAdmin:{rowActions:{firstAction:n.default,moreActions:[]},"
    "batchActions:[n.default],primaryActions:[]}}"
    ";t.default=s},1697:function(e,t,a){nextmodule}"
    # ClustersStore with upgrade method (separate module)
    "1344:function(e,t,a){"
    'upgrade(e,t){var a=this;return(0,g.default)((function*()'
    "{var{id:r}=e;return a.client.upgrade(r,t)}))()}"
    "}"
)


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


class TestPatchKubeconfig:
    """_patch_magnum_kubeconfig() adds Download Kubeconfig button."""

    def test_adds_config_extend_operation(self, harness_installed, static_dir):
        """Main bundle gets the config generate function in MagnumClient."""
        make_main_bundle_file(static_dir, content=_FAKE_MAIN_BUNDLE)
        harness_installed.charm._patch_magnum_kubeconfig()
        result = (static_dir / "main.bundle.1786807402.js").read_text()
        assert 'name:"config"' in result
        assert 'fetch(' in result

    def test_injects_new_module_9999(self, harness_installed, static_dir):
        """Container-infra bundle gets new module 9999 with DownloadKubeconfig."""
        make_bundle_file(static_dir, content=_FAKE_CI_BUNDLE)
        harness_installed.charm._patch_magnum_kubeconfig()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert "9999:function(e,a,r)" in result
        assert "download-kubeconfig" in result
        assert "ConfirmAction" in result

    def test_wires_module_9999_into_module_1696(self, harness_installed, static_dir):
        """Module 1696 imports module 9999 and adds it to moreActions."""
        make_bundle_file(static_dir, content=_FAKE_CI_BUNDLE)
        harness_installed.charm._patch_magnum_kubeconfig()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert "d=l(a(9999))" in result
        assert "moreActions:[{action:d.default},{action:i.default}]" in result

    def test_adds_config_store_method(self, harness_installed, static_dir):
        """Container-infra bundle gets config() method on ClustersStore."""
        make_bundle_file(static_dir, content=_FAKE_CI_BUNDLE)
        harness_installed.charm._patch_magnum_kubeconfig()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert 'config(e){var a=this;return(0,g.default)' in result
        assert "a.client.config(r)" in result

    def test_idempotent(self, harness_installed, static_dir):
        """Already-patched files are not double-patched."""
        make_main_bundle_file(static_dir, content=_FAKE_MAIN_BUNDLE)
        make_bundle_file(static_dir, content=_FAKE_CI_BUNDLE)
        harness_installed.charm._patch_magnum_kubeconfig()
        harness_installed.charm._patch_magnum_kubeconfig()
        main_result = (static_dir / "main.bundle.1786807402.js").read_text()
        ci_result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert main_result.count('name:"config"') == 1
        assert ci_result.count("9999:function") == 1

    def test_broadens_allowed_checkfunc_on_injected_module(self, harness_installed, static_dir):
        """Fresh injection uses the broadened allowedCheckFunc (all healthy
        completion states), not CREATE_COMPLETE-only."""
        make_bundle_file(static_dir, content=_FAKE_CI_BUNDLE)
        harness_installed.charm._patch_magnum_kubeconfig()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        assert 'return"CREATE_COMPLETE"===e.status' not in result
        assert "/^(?:CREATE|UPDATE|ROLLBACK|RESUME|RESTORE|SNAPSHOT|ADOPT|CHECK)_COMPLETE$/".replace("_COMPLETE$", "_COMPLETE$") in result

    def test_upgrades_already_patched_old_guard(self, harness_installed, static_dir):
        """An already-patched bundle (module 9999 present with the OLD
        CREATE_COMPLETE-only guard) is upgraded in place to the broadened
        allowedCheckFunc."""
        # Simulate the deployed (old) patched bundle: module 9999 exists with
        # the CREATE_COMPLETE-only allowedCheckFunc, plus existing wiring so
        # the sub-patches (store/imports/moreActions/module-injection) are no-ops.
        old_guard = 'allowedCheckFunc",(function(e){return"CREATE_COMPLETE"===e.status})'
        old_patched = (
            '1696:function(e,t,a){"use strict";var r=a(20),l=a(21);'
            'r(t,"__esModule",{value:!0}),t.default=void 0;'
            "var n=l(a(4307)),i=l(a(4308)),o=l(a(1488)),d=l(a(9999)),s={"
            "actionConfigs:{rowActions:{firstAction:n.default,moreActions:[{action:d.default},{action:i.default}]},"
            "batchActions:[n.default],primaryActions:[o.default]},"
            "actionConfigsAdmin:{rowActions:{firstAction:n.default,moreActions:[]},"
            "batchActions:[n.default],primaryActions:[]}}"
            ";t.default=s},"
            '9999:function(e,a,r){"use strict";var i=n(r(35)),o=r(1244),s=n(r(1344));'
            "class u extends o.ConfirmAction{constructor(){super(...arguments),"
            + old_guard + ','
            "(0,i.default)(this,\"onSubmit\",\"fn\")}"
            'get id(){return"download-kubeconfig"}'
            'get buttonText(){return"Download kubeconfig"}'
            "get isDanger(){return!1}}},"
            '1697:function(e,t,a){nextmodule}'
        )
        make_bundle_file(static_dir, content=old_patched)
        harness_installed.charm._patch_magnum_kubeconfig()
        result = (static_dir / "container-infra.bundle.1786807402.js").read_text()
        # Old guard gone, broadened regex present, exactly one module
        assert 'return"CREATE_COMPLETE"===e.status' not in result
        assert "/^(?:CREATE|UPDATE|ROLLBACK|RESUME|RESTORE|SNAPSHOT|ADOPT|CHECK)_COMPLETE$/" in result
        assert result.count("9999:function") == 1

    def test_noop_when_static_path_empty(self, harness):
        """No-op when static_path is not yet discovered."""
        harness.begin()
        harness.charm._stored.static_path = ""
        harness.charm._patch_magnum_kubeconfig()

    def test_removes_all_gz_files(self, harness_installed, static_dir):
        """All .gz files in static dir are deleted after patching."""
        main_path = make_main_bundle_file(static_dir, content=_FAKE_MAIN_BUNDLE)
        ci_path = make_bundle_file(static_dir, content=_FAKE_CI_BUNDLE)
        main_gz = main_path.with_suffix(main_path.suffix + ".gz")
        ci_gz = ci_path.with_suffix(ci_path.suffix + ".gz")
        other_gz = static_dir / "some-other-file.js.gz"
        main_gz.write_bytes(b"fake")
        ci_gz.write_bytes(b"fake")
        other_gz.write_bytes(b"fake")
        harness_installed.charm._patch_magnum_kubeconfig()
        assert not main_gz.exists()
        assert not ci_gz.exists()
        assert not other_gz.exists()


class TestRemoveStaleGzFiles:
    """_remove_stale_gz_files() deletes every .gz companion file."""

    def test_removes_all_gz_in_dir(self, harness_installed, static_dir):
        """Every .gz file is removed; .js files are left untouched."""
        (static_dir / "a.js").write_text("a", encoding="utf-8")
        (static_dir / "a.js.gz").write_bytes(b"g")
        (static_dir / "b.js.gz").write_bytes(b"g")
        (static_dir / "helper.min.js.gz").write_bytes(b"g")
        (static_dir / "style.css").write_text("c", encoding="utf-8")
        removed = harness_installed.charm._remove_stale_gz_files(static_dir)
        assert removed == 3
        assert (static_dir / "a.js").exists()
        assert (static_dir / "style.css").exists()
        assert not list(static_dir.glob("*.gz"))

    def test_returns_zero_when_none(self, harness_installed, static_dir):
        """Returns 0 when the dir holds no .gz files."""
        assert harness_installed.charm._remove_stale_gz_files(static_dir) == 0


class TestPatchKubeconfigEndpoint:
    """_patch_kubeconfig_endpoint() injects the FastAPI endpoint + router wiring."""

    def test_writes_endpoint_file_and_patches_init(self, harness_installed, tmp_path):
        """Endpoint file is written and __init__.py wired (import + include)."""
        venv_lib = make_apiserver_venv(tmp_path)
        harness_installed.charm._patch_kubeconfig_endpoint(venv_lib=venv_lib)
        v1_dir = venv_lib / "python3.10" / "site-packages" / "skyline_apiserver" / "api" / "v1"
        kubeconfig_py = v1_dir / "kubeconfig.py"
        init_file = v1_dir / "__init__.py"
        assert kubeconfig_py.exists()
        endpoint_src = kubeconfig_py.read_text(encoding="utf-8")
        assert "# skyline-charm: kubeconfig endpoint v4" in endpoint_src
        init_text = init_file.read_text(encoding="utf-8")
        assert "kubeconfig" in init_text
        assert 'include_router(kubeconfig.router, tags=["Kubeconfig"])' in init_text

    def test_idempotent(self, harness_installed, tmp_path):
        """Running twice leaves a single router include and no double imports."""
        venv_lib = make_apiserver_venv(tmp_path)
        harness_installed.charm._patch_kubeconfig_endpoint(venv_lib=venv_lib)
        harness_installed.charm._patch_kubeconfig_endpoint(venv_lib=venv_lib)
        v1_dir = venv_lib / "python3.10" / "site-packages" / "skyline_apiserver" / "api" / "v1"
        init_text = (v1_dir / "__init__.py").read_text(encoding="utf-8")
        assert init_text.count('include_router(kubeconfig.router') == 1
        assert init_text.count("kubeconfig,") == 1

    def test_updates_stale_endpoint_file(self, harness_installed, tmp_path):
        """An old endpoint file without the v3 marker is refreshed."""
        venv_lib = make_apiserver_venv(tmp_path)
        v1_dir = venv_lib / "python3.10" / "site-packages" / "skyline_apiserver" / "api" / "v1"
        (v1_dir / "kubeconfig.py").write_text("OLD_CONTENT", encoding="utf-8")
        harness_installed.charm._patch_kubeconfig_endpoint(venv_lib=venv_lib)
        assert "OLD_CONTENT" not in (v1_dir / "kubeconfig.py").read_text(encoding="utf-8")

    def test_noop_when_package_missing(self, harness_installed, tmp_path):
        """Skipped cleanly when the apiserver package is not installed."""
        empty_lib = tmp_path / "empty-lib"
        empty_lib.mkdir()
        harness_installed.charm._patch_kubeconfig_endpoint(venv_lib=empty_lib)

    def test_configure_calls_endpoint_patch(self, harness_installed, tmp_path, monkeypatch):
        """_configure() runs the kubeconfig endpoint patch each time."""
        venv_lib = make_apiserver_venv(tmp_path)
        charm = harness_installed.charm
        real_patch = SkylineCharm._patch_kubeconfig_endpoint
        monkeypatch.setattr(charm, "_missing_required_config", lambda: None)
        monkeypatch.setattr(charm, "_patch_container_infra_bundle", lambda: None)
        monkeypatch.setattr(charm, "_publish_shared_db_request", lambda: None)
        monkeypatch.setattr(charm, "_shared_db_related", lambda: True)
        monkeypatch.setattr(charm, "_shared_db_data", lambda: None)
        monkeypatch.setattr(type(charm), "_patch_kubeconfig_endpoint",
                            lambda self, venv_lib=venv_lib: real_patch(self, venv_lib=venv_lib))
        charm._configure()
        v1_dir = venv_lib / "python3.10" / "site-packages" / "skyline_apiserver" / "api" / "v1"
        assert (v1_dir / "kubeconfig.py").exists()
        assert "kubeconfig" in (v1_dir / "__init__.py").read_text(encoding="utf-8")


class TestKubeconfigEndpointSource:
    """The embedded endpoint source (injected into the apiserver) stays valid Python."""

    def test_endpoint_source_compiles(self):
        """A malformed edit to _KUBECONFIG_ENDPOINT_SRC must fail tests, not the apiserver."""
        compile(_KUBECONFIG_ENDPOINT_SRC, "<endpoint>", "exec")

    def test_endpoint_handler_is_sync_and_no_hardcoded_url(self):
        """Blocking calls must run on a worker thread; no deployment-specific URL."""
        src = _KUBECONFIG_ENDPOINT_SRC
        assert "async def get_cluster_kubeconfig" not in src
        assert "def get_cluster_kubeconfig(cluster_id" in src
        assert "10.11.1.48" not in src
