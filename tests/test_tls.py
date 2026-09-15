"""Tests for TLS: certificates relation, nginx TLS rendering, cafile guard."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import charm as charm_module

CERT = "-----BEGIN CERTIFICATE-----\nserver\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----\n"
CA = "-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n"


def _certs_data(ca=CA):
    return {"certificate": CERT, "private_key": KEY, "ca": ca, "chain": ""}


@pytest.fixture
def cert_paths(tmp_path, monkeypatch):
    """Point the charm's TLS paths at a temp dir (never touch /etc)."""
    certs_dir = tmp_path / "certs"
    monkeypatch.setattr(charm_module, "CERTS_DIR", certs_dir)
    monkeypatch.setattr(charm_module, "TLS_CERT_PATH", certs_dir / "server.crt")
    monkeypatch.setattr(charm_module, "TLS_KEY_PATH", certs_dir / "server.key")
    monkeypatch.setattr(charm_module, "TLS_CA_PATH", certs_dir / "vault-ca.crt")
    monkeypatch.setattr(charm_module, "SYSTEM_CA_PATH", tmp_path / "sys-ca.crt")
    return certs_dir


def _add_certificate_relation(harness, provider="vault/0", data=None):
    rel_id = harness.add_relation("certificates", "vault")
    harness.add_relation_unit(rel_id, provider)
    if data:
        harness.update_relation_data(rel_id, provider, data)
    return rel_id


GENERATED_NGINX = (
    "http {\n"
    "    upstream skyline {\n"
    "        server 127.0.0.1:28000 fail_timeout=0;\n"
    "    }\n"
    "    server {\n"
    "        listen 0.0.0.0:9999 default_server;\n"
    "        server_name _;\n"
    "        location / {\n"
    "            try_files $uri $uri/ /index.html;\n"
    "        }\n"
    "    }\n"
    "}\n"
)


class TestCertificatesData:
    def test_none_without_relation(self, harness_installed):
        assert harness_installed.charm._certificates_data() is None
        assert harness_installed.charm._tls_enabled() is False

    def test_none_when_incomplete(self, harness_installed):
        _add_certificate_relation(
            harness_installed, data={"ca": CA, "skyline_0.server.cert": CERT},
        )
        assert harness_installed.charm._certificates_data() is None

    def test_returns_material(self, harness_installed):
        _add_certificate_relation(harness_installed, data={
            "ca": CA,
            "skyline_0.server.cert": CERT,
            "skyline_0.server.key": KEY,
        })
        data = harness_installed.charm._certificates_data()
        assert data["certificate"] == CERT
        assert data["private_key"] == KEY
        assert data["ca"] == CA
        assert harness_installed.charm._tls_enabled() is True


class TestPublishCertificateRequest:
    def test_publishes_request_keys(self, harness_installed):
        rel_id = _add_certificate_relation(harness_installed)
        harness_installed.charm._publish_certificate_request()
        data = harness_installed.get_relation_data(rel_id, "skyline/0")
        assert data["unit_name"] == "skyline_0"
        assert data["common_name"]
        assert data["certificate_name"]
        sans = json.loads(data["sans"])
        assert isinstance(sans, list) and sans

    def test_certificate_name_is_stable(self, harness_installed):
        rel_id = _add_certificate_relation(harness_installed)
        harness_installed.charm._publish_certificate_request()
        first = harness_installed.get_relation_data(rel_id, "skyline/0")
        harness_installed.charm._publish_certificate_request()
        second = harness_installed.get_relation_data(rel_id, "skyline/0")
        assert first["certificate_name"] == second["certificate_name"]


class TestInstallCertificates:
    def test_writes_material_and_ca(self, harness_installed, cert_paths):
        assert harness_installed.charm._install_certificates(_certs_data()) is True
        assert (cert_paths / "server.crt").read_text() == CERT
        assert (cert_paths / "server.key").read_text() == KEY
        assert (cert_paths / "vault-ca.crt").read_text() == CA
        assert harness_installed.charm._stored.cert_fingerprint

    def test_idempotent(self, harness_installed, cert_paths):
        harness_installed.charm._install_certificates(_certs_data())
        assert harness_installed.charm._install_certificates(_certs_data()) is False

    def test_rewrites_on_change(self, harness_installed, cert_paths):
        harness_installed.charm._install_certificates(_certs_data())
        changed = _certs_data()
        changed["certificate"] = CERT.replace("server", "server2")
        assert harness_installed.charm._install_certificates(changed) is True
        assert "server2" in (cert_paths / "server.crt").read_text()

    def test_update_ca_certificates_only_on_ca_change(
        self, harness_installed, cert_paths
    ):
        calls = []
        harness_installed.charm._run = lambda cmd, *a, **k: calls.append(cmd)
        harness_installed.charm._install_certificates(_certs_data())
        assert ["update-ca-certificates"] in calls
        calls.clear()
        changed = _certs_data()
        changed["certificate"] = CERT.replace("server", "server3")
        harness_installed.charm._install_certificates(changed)
        assert ["update-ca-certificates"] not in calls


class TestEffectiveCafile:
    def test_explicit_path_used(self, harness_installed, tmp_path):
        ca_file = tmp_path / "custom-ca.pem"
        ca_file.write_text(CA)
        harness_installed.update_config({"cafile": str(ca_file)})
        assert harness_installed.charm._effective_cafile(
            harness_installed.charm._effective_identity(), None
        ) == str(ca_file)

    def test_explicit_missing_path_is_empty(self, harness_installed, tmp_path):
        harness_installed.update_config({"cafile": str(tmp_path / "nope.pem")})
        assert harness_installed.charm._effective_cafile(
            harness_installed.charm._effective_identity(), None
        ) == ""

    def test_relation_ca_used_when_probe_ok(
        self, harness_installed, cert_paths
    ):
        harness_installed.charm._install_certificates(_certs_data())
        harness_installed.charm._run = lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"ok": True, "checked": 4, "failures": []}),
        )
        result = harness_installed.charm._effective_cafile(
            harness_installed.charm._effective_identity(), _certs_data()
        )
        assert result == str(cert_paths / "vault-ca.crt")

    def test_relation_ca_skipped_when_probe_fails(
        self, harness_installed, cert_paths
    ):
        harness_installed.charm._install_certificates(_certs_data())
        failures = ["https://glance:9292: certificate verify failed"]
        harness_installed.charm._run = lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"ok": False, "checked": 4, "failures": failures}),
        )
        result = harness_installed.charm._effective_cafile(
            harness_installed.charm._effective_identity(), _certs_data()
        )
        assert result == ""
        stored = json.loads(harness_installed.charm._stored.tls_probe_failures)
        assert stored == failures

    def test_probe_result_is_memoized_per_hook(self, harness_installed, cert_paths):
        harness_installed.charm._install_certificates(_certs_data())
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(
                    {"ok": True, "checked": 1, "failures": []}
                ),
            )

        harness_installed.charm._run = fake_run
        identity = harness_installed.charm._effective_identity()
        harness_installed.charm._effective_cafile(identity, _certs_data())
        assert len(calls) == 1
        harness_installed.charm._effective_cafile(identity, _certs_data())
        assert len(calls) == 1
        # a fresh hook (memo cleared) probes again
        harness_installed.charm._cafile_probe = None
        harness_installed.charm._effective_cafile(identity, _certs_data())
        assert len(calls) == 2

    def test_empty_ca_bundle_skips_system_install(
        self, harness_installed, cert_paths
    ):
        calls = []
        harness_installed.charm._run = lambda cmd, *a, **k: calls.append(cmd)
        assert harness_installed.charm._install_certificates(
            _certs_data(ca="")
        ) is True
        assert not (cert_paths / "vault-ca.crt").exists()
        assert ["update-ca-certificates"] not in calls


class TestInjectTls:
    def test_noop_without_certificate(self, harness_installed):
        assert harness_installed.charm._inject_tls(GENERATED_NGINX) == GENERATED_NGINX

    def test_injects_ssl_listener_and_redirect(self, harness_installed):
        harness_installed.charm._certificates_data = lambda: _certs_data()
        out = harness_installed.charm._inject_tls(GENERATED_NGINX)
        assert "listen 0.0.0.0:443 ssl default_server;" in out
        assert "listen [::]:443 ssl default_server;" in out
        assert f"ssl_certificate {charm_module.TLS_CERT_PATH};" in out
        assert f"ssl_certificate_key {charm_module.TLS_KEY_PATH};" in out
        assert "return 301 https://$host$request_uri;" in out
        assert "listen 0.0.0.0:9999;" in out
        assert "listen 0.0.0.0:9999 default_server;" not in out

    def test_idempotent(self, harness_installed):
        harness_installed.charm._certificates_data = lambda: _certs_data()
        once = harness_installed.charm._inject_tls(GENERATED_NGINX)
        assert harness_installed.charm._inject_tls(once) == once

    def test_redirect_can_be_disabled(self, harness_installed):
        harness_installed.update_config({"tls-redirect": False})
        harness_installed.charm._certificates_data = lambda: _certs_data()
        out = harness_installed.charm._inject_tls(GENERATED_NGINX)
        assert "return 301" not in out
        assert "listen 0.0.0.0:443 ssl default_server;" in out

    def test_redirect_skipped_when_ports_equal(self, harness_installed):
        harness_installed.update_config({"listen-port": 443})
        harness_installed.charm._certificates_data = lambda: _certs_data()
        out = harness_installed.charm._inject_tls(GENERATED_NGINX)
        assert "return 301" not in out
        assert "listen 0.0.0.0:443 ssl default_server;" in out

    def test_no_listen_anchor_leaves_content(self, harness_installed):
        harness_installed.charm._certificates_data = lambda: _certs_data()
        content = "http {\n    server {\n        server_name _;\n    }\n}\n"
        assert harness_installed.charm._inject_tls(content) == content


class TestNginxFallbackTemplate:
    def test_http_when_no_tls(self, harness_installed, tmp_path):
        dest = tmp_path / "nginx.conf"
        harness_installed.charm._render_template(
            "nginx.conf.j2", dest, harness_installed.charm._template_context()
        )
        text = dest.read_text()
        assert "listen 9999 default_server;" in text
        assert "ssl_certificate" not in text
        assert "return 301" not in text

    def test_tls_renders_listener_and_redirect(self, harness_installed, tmp_path):
        harness_installed.charm._certificates_data = lambda: _certs_data()
        dest = tmp_path / "nginx.conf"
        harness_installed.charm._render_template(
            "nginx.conf.j2", dest, harness_installed.charm._template_context()
        )
        text = dest.read_text()
        assert "listen 443 ssl default_server;" in text
        assert f"ssl_certificate {charm_module.TLS_CERT_PATH};" in text
        assert "return 301 https://$host$request_uri;" in text


class TestSkylineYamlTemplate:
    def test_cafile_rendered(self, harness_installed, tmp_path):
        dest = tmp_path / "skyline.yaml"
        context = harness_installed.charm._template_context()
        context["cafile"] = "/etc/skyline/certs/vault-ca.crt"
        harness_installed.charm._render_template(
            "skyline.yaml.j2", dest, context
        )
        assert "cafile: '/etc/skyline/certs/vault-ca.crt'" in dest.read_text()

    def test_ssl_enabled_forced_by_tls(self, harness_installed):
        context = harness_installed.charm._template_context()
        assert context["ssl_enabled"] is False
        harness_installed.charm._certificates_data = lambda: _certs_data()
        context = harness_installed.charm._template_context()
        assert context["ssl_enabled"] is True


class TestWebsiteAndPorts:
    def test_website_publishes_tls_port(self, harness_installed):
        harness_installed.set_leader(True)
        rel_id = harness_installed.add_relation("website", "haproxy")
        harness_installed.add_relation_unit(rel_id, "haproxy/0")
        harness_installed.charm._certificates_data = lambda: _certs_data()
        relation = harness_installed.charm.model.get_relation("website", rel_id)
        harness_installed.charm._publish_website(relation)
        data = harness_installed.get_relation_data(rel_id, "skyline/0")
        assert data["port"] == "443"

    def test_tls_port_opened(self, harness_installed):
        opened = []
        harness_installed.charm.unit.open_port = (
            lambda proto, port: opened.append((proto, port))
        )
        harness_installed.charm._certificates_data = lambda: _certs_data()
        harness_installed.charm._open_listen_port()
        assert ("tcp", 443) in opened

    def test_tls_port_not_opened_without_certs(self, harness_installed):
        opened = []
        harness_installed.charm.unit.open_port = (
            lambda proto, port: opened.append((proto, port))
        )
        harness_installed.charm._open_listen_port()
        assert ("tcp", 443) not in opened
        assert ("tcp", 9999) in opened


class TestCertificatesHandler:
    def test_defers_before_install(self, harness):
        harness.begin()
        harness.charm._stored.installed = False
        rel_id = _add_certificate_relation(harness)
        relation = harness.charm.model.get_relation("certificates", rel_id)
        harness.charm.on["certificates"].relation_changed.emit(relation)
        assert harness.charm.unit.status.name == "waiting"

    def test_activates_when_configure_ok(self, harness_installed):
        rel_id = _add_certificate_relation(harness_installed)
        with patch.object(harness_installed.charm, "_configure", return_value=True):
            relation = harness_installed.charm.model.get_relation(
                "certificates", rel_id
            )
            harness_installed.charm.on["certificates"].relation_changed.emit(relation)
            assert harness_installed.charm.unit.status.name == "active"

    def test_status_message_is_unit_is_ready(self, harness_installed):
        rel_id = _add_certificate_relation(harness_installed)
        with patch.object(harness_installed.charm, "_configure", return_value=True):
            relation = harness_installed.charm.model.get_relation(
                "certificates", rel_id
            )
            harness_installed.charm.on["certificates"].relation_changed.emit(relation)
            assert harness_installed.charm.unit.status.message == "Unit is ready"


class TestCheckTlsAction:
    def test_no_ca_available(self, harness_installed, cert_paths):
        out = harness_installed.run_action("check-tls")
        assert out.results["ok"] is False
        assert out.results["cafile"] == "(none)"
        assert "no CA available" in out.results["failures"]

    def test_with_relation_ca(self, harness_installed, cert_paths):
        harness_installed.charm._install_certificates(_certs_data())
        harness_installed.charm._run = lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"ok": True, "checked": 5, "failures": []}),
        )
        out = harness_installed.run_action("check-tls")
        assert out.results["ok"] is True
        assert out.results["checked"] == 5
        assert out.results["cafile"].endswith("vault-ca.crt")

    def test_with_explicit_cafile(self, harness_installed, cert_paths, tmp_path):
        ca_file = tmp_path / "custom-ca.pem"
        ca_file.write_text(CA)
        harness_installed.update_config({"cafile": str(ca_file)})
        harness_installed.charm._run = lambda cmd, *a, **k: subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"ok": False, "checked": 2,
                               "failures": ["https://glance:9292: bad cert"]}),
        )
        out = harness_installed.run_action("check-tls")
        assert out.results["ok"] is False
        assert out.results["cafile"] == str(ca_file)
        assert "glance" in out.results["failures"]
