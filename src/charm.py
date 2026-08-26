#!/usr/bin/env python3
# Copyright 2024 OpenStack Operator
# SPDX-License-Identifier: Apache-2.0
"""
Juju charm for OpenStack Skyline Dashboard (stable/2024.2).

The skyline-console wheel is bundled inside the charm (files/ directory)
and installed directly — no Node.js, nvm, yarn or make build at deploy time.

Install sequence
---------------
1.  apt: baseline packages + (optional) mariadb
2.  python3 -m venv /opt/skyline-venv
3.  install skyline-apiserver from the prebuilt wheel + all pinned deps from
    files/wheels (fully offline: --no-index --find-links; PIP_NO_INDEX=1)
4.  install bundled skyline-console wheel from files/wheels (offline)
5.  Discover and store console static path

config-changed
-------------
Re-renders all templates, re-runs db_sync, reloads/restarts services.

nginx config
------------
/etc/nginx/nginx.conf is generated from the keystone catalog using the
shipped `skyline-nginx-generator` (skyline_apiserver.cmd.generate_nginx):
it emits the /api/openstack/skyline/ strip proxy plus one proxy location per
cataloged OpenStack service (/api/openstack/<region>/<service>/ -> the real
service endpoint), which is how the console's overview/admin pages reach
keystone/nova/cinder/etc. If the generator fails (e.g. keystone unreachable
at config time), the static templates/nginx.conf.j2 is rendered instead and
the unit stays active; re-run with the regenerate-nginx action or a juju
config change once keystone is reachable.


"""

import logging
import os
import re
import secrets
import shutil
import subprocess
import textwrap
import tarfile
import time
from pathlib import Path
from urllib.parse import quote

import ops
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

# ── Filesystem paths ────────────────────────────────────────────────────────
VENV_DIR = Path("/opt/skyline-venv")
VENV_BIN = VENV_DIR / "bin"
VENV_PY  = VENV_BIN / "python3"
VENV_PIP = VENV_BIN / "pip"

APISERVER_SRC = Path("/opt/skyline-apiserver-src")

SKYLINE_CONF_DIR   = Path("/etc/skyline")
SKYLINE_LOG_DIR    = Path("/var/log/skyline")
SKYLINE_POLICY_DIR = SKYLINE_CONF_DIR / "policy"

SYSTEMD_UNIT_PATH  = Path("/etc/systemd/system/skyline-apiserver.service")
NGINX_CONF_PATH    = Path("/etc/nginx/nginx.conf")
GUNICORN_CONF_PATH = SKYLINE_CONF_DIR / "gunicorn.py"
SKYLINE_YAML_PATH  = SKYLINE_CONF_DIR / "skyline.yaml"
GENERATED_NGINX_PATH = SKYLINE_CONF_DIR / "nginx.conf.generated"

# Local MariaDB TCP port. Deliberately outside the mysql-router's 3306-3309
# range so a co-located router can always bind 3306 regardless of hook order.
LOCAL_MARIADB_PORT = 13306
MARIADB_CNF_PATH   = Path("/etc/mysql/mariadb.conf.d/60-skyline.cnf")


class SkylineCharm(ops.CharmBase):
    """Juju charm deploying the OpenStack Skyline Dashboard."""

    _stored = ops.StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self._stored.set_default(
            installed=False,
            secret_key="",
            db_password="",
            static_path="",
            opened_port=0,
        )
        self.framework.observe(self.on.install,        self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.start,          self._on_start)
        self.framework.observe(self.on.upgrade_charm,  self._on_upgrade_charm)

        self.framework.observe(self.on["shared-db"].relation_created,
                               self._on_shared_db_created)
        self.framework.observe(self.on["shared-db"].relation_changed,
                               self._on_shared_db_changed)
        self.framework.observe(self.on["shared-db"].relation_broken,
                               self._on_shared_db_changed)
        self.framework.observe(self.on["skyline-peers"].relation_changed,
                               self._on_peers_changed)

        self.framework.observe(self.on["website"].relation_joined,
                               self._on_website_joined)
        self.framework.observe(self.on["website"].relation_changed,
                               self._on_website_changed)

        self.framework.observe(self.on.db_sync_action,          self._on_action_db_sync)
        self.framework.observe(self.on.get_static_path_action,  self._on_action_get_static_path)
        self.framework.observe(self.on.restart_services_action, self._on_action_restart_services)
        self.framework.observe(self.on.show_config_action,      self._on_action_show_config)
        self.framework.observe(self.on.regenerate_nginx_action, self._on_action_regenerate_nginx)
        self.framework.observe(self.on.patch_frontend_action, self._on_action_patch_frontend)

    # ── Low-level helpers ───────────────────────────────────────────────────

    def _run(self, cmd, input_data=None, env=None, cwd=None, capture=False,
             check=True, timeout=None):
        logger.debug("run: %s", " ".join(str(x) for x in cmd))
        kwargs = dict(check=check, cwd=cwd, env=env)
        if input_data is not None:
            kwargs["input"] = input_data
        if capture:
            kwargs["capture_output"] = True
            kwargs["text"] = True
        if timeout is not None:
            kwargs["timeout"] = timeout
        return subprocess.run(cmd, **kwargs)

    def _apt_install(self, packages: list):
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        self._run(["apt-get", "update", "-qq"], env=env)
        self._run(["apt-get", "install", "-y", "--no-install-recommends"] + packages, env=env)

    def _pip(self, args: list, env=None):
        self._run([str(VENV_PIP)] + args, env=env)

    @staticmethod
    def _safe_extract(tar: tarfile.TarFile, dest: Path):
        """Extract a tar archive, rejecting any member that would escape dest."""
        dest_resolved = dest.resolve()
        for member in tar.getmembers():
            target = (dest_resolved / member.name).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise RuntimeError(
                    f"Refusing to extract path outside {dest}: {member.name}"
                )
        tar.extractall(path=dest)

    def _render_template(self, template_name: str, dest: Path, context: dict):
        tmpl_dir = Path(self.charm_dir) / "templates"
        env = Environment(
            loader=FileSystemLoader(str(tmpl_dir)),
            autoescape=False,
            keep_trailing_newline=True,
        )
        content = env.get_template(template_name).render(**context)
        dest.write_text(content, encoding="utf-8")
        logger.info("Rendered %s -> %s", template_name, dest)

    def _venv_env(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}"
        env["OS_CONFIG_DIR"] = str(SKYLINE_CONF_DIR)
        env["VIRTUAL_ENV"]   = str(VENV_DIR)
        return env

    def _wheels_dir(self) -> Path:
        return Path(self.charm_dir) / "files" / "wheels"

    # ── Config helpers ──────────────────────────────────────────────────────

    def _shared_db_data(self):
        """
        Connection info from the mysql-router ``shared-db`` relation.

        Compatible with the mysql-router (canonical) subordinate, which provides
        per-unit keys db_host/db_port/username/password/database. Older
        mysql-shared providers used host/port/user/db_name — both spellings are
        tolerated. Returns None until the router has published usable data.
        """
        relations = self.model.relations.get("shared-db") or []
        for relation in relations:
            candidates = []
            if relation.app is not None:
                candidates.append(relation.data[relation.app])
            candidates.extend(relation.data[unit] for unit in relation.units)
            for data in candidates:
                d = dict(data)
                host = d.get("db_host") or d.get("host")
                if not host:
                    continue
                return {
                    "host": host,
                    "port": d.get("db_port") or d.get("port") or "3306",
                    "username": d.get("username") or d.get("user") or "skyline",
                    "password": d.get("password") or "",
                    "database": d.get("database") or d.get("db_name") or "skyline",
                }
        return None

    def _publish_shared_db_request(self) -> None:
        """
        Requirer half of the mysql-shared contract: advertise which database
        and user the mysql-router must create on the cluster.

        The router reads a singleset {database, username, hostname} from our
        shared-db relation data, proxies it to mysql-innodb-cluster as
        MRUP_* keys, and once the cluster provisions the DB/user it publishes
        db_host/db_port/username/password back for ``_shared_db_data`` to pick
        up. Idempotent; no-op when the relation is absent or unbound.
        """
        for rel in self.model.relations.get("shared-db") or []:
            try:
                hostname = str(self.model.get_binding(rel).network.ingress_address)
            except Exception as exc:
                logger.warning("shared-db: cannot determine hostname: %s", exc)
                continue
            want = {"database": "skyline", "username": "skyline", "hostname": hostname}
            unit_data = rel.data[self.unit]
            if any(unit_data.get(k) != v for k, v in want.items()):
                unit_data.update(want)
                logger.info("published shared-db DB request: %s", want)

    def _shared_db_related(self) -> bool:
        """
        True while at least one mysql-router unit is attached to the
        ``shared-db`` relation (remote units present). A dying/detached
        relation has no remote units, so detaching cleanly falls back to the
        previous DB mode.
        """
        for rel in self.model.relations.get("shared-db") or []:
            if rel.units:
                return True
        return False

    def _using_local_db(self) -> bool:
        if self._shared_db_data():
            return False
        if self._shared_db_related():
            # A mysql-router subordinate is attached (credentials may still be
            # in flight). Never fall back to a local MariaDB: it would race the
            # router for 127.0.0.1:3306 and wedge its bootstrap.
            return False
        return not bool(self.config.get("database-url", "").strip())

    def _db_password(self) -> str:
        if not self._stored.db_password:
            cfg = self.config.get("database-password", "").strip()
            self._stored.db_password = cfg if cfg else secrets.token_urlsafe(20)
        return self._stored.db_password

    def _secret_key(self) -> str:
        """Return the session secret every unit must share.

        Priority: explicit ``secret-key`` config (operator-defined seed /
        rotation) > value published by the leader over ``skyline-peers`` > the
        unit's own stored (or freshly generated) key.
        """
        cfg = self.config.get("secret-key", "").strip()
        if cfg:
            return cfg
        peers = self.model.relations.get("skyline-peers") or []
        if peers:
            relation = peers[0]
            if relation.app is not None:
                peer_key = relation.data[self.model.app].get("secret_key", "")
                if peer_key:
                    return peer_key
        if not self._stored.secret_key:
            self._stored.secret_key = secrets.token_urlsafe(32)
        return self._stored.secret_key

    def _publish_secret_key(self) -> None:
        """
        Ensure a single uniform session secret across all skyline units.

        The leader owns the auto-generated key and publishes it in the
        ``skyline-peers`` app databag so scaled-out units render the same
        ``secret_key`` (required for HA — sessions are signed with it). An
        explicit ``secret-key`` config value always wins (seed or rotation).
        """
        cfg = self.config.get("secret-key", "").strip()
        if cfg:
            effective = cfg
        else:
            if not self._stored.secret_key:
                self._stored.secret_key = secrets.token_urlsafe(32)
            effective = self._stored.secret_key

        peers = self.model.relations.get("skyline-peers") or []
        if not peers or not self.unit.is_leader():
            return
        relation = peers[0]
        if relation.app is None:
            return
        app_data = relation.data[self.model.app]
        if app_data.get("secret_key") != effective:
            app_data["secret_key"] = effective
            logger.info("Published uniform secret_key to skyline-peers")

    def _database_url(self) -> str:
        shared = self._shared_db_data()
        if shared:
            return "mysql://{user}:{passwd}@{host}:{port}/{db}".format(
                user=quote(shared["username"], safe=""),
                passwd=quote(shared["password"], safe=""),
                host=shared["host"],
                port=shared["port"],
                db=quote(shared["database"], safe=""),
            )
        if not self._using_local_db():
            return self.config["database-url"].strip()
        # Local MariaDB is deliberately OFF the router's port range: it binds
        # 127.0.0.1:13306 (see _setup_local_mariadb), leaving 127.0.0.1:3306
        # to the co-located mysql-router subordinate. Hook timing can never
        # put the two in each other's way.
        return f"mysql://skyline:{self._db_password()}@localhost:{LOCAL_MARIADB_PORT}/skyline"

    def _keystone_url(self) -> str:
        url = self.config.get("keystone-url", "").strip().rstrip("/")
        if not url.endswith("/v3"):
            url += "/v3"
        return url + "/"

    def _missing_required_config(self) -> str:
        if not self.config.get("keystone-url", "").strip():
            return "Required config 'keystone-url' is not set"
        if not self.config.get("system-user-password", "").strip():
            return "Required config 'system-user-password' is not set"
        return ""

    def _template_context(self) -> dict:
        cfg = self.config
        workers = cfg["gunicorn-workers"]
        return {
            "database_url":                   self._database_url(),
            "keystone_url":                   self._keystone_url(),
            "default_region":                 cfg["default-region"],
            "system_user_name":               cfg["system-user-name"],
            "system_user_password":           cfg["system-user-password"],
            "system_user_domain":             cfg["system-user-domain"],
            "system_project":                 cfg["system-project"],
            "system_project_domain":          cfg["system-project-domain"],
            "interface_type":                 cfg["interface-type"],
            "sso_enabled":                    cfg["sso-enabled"],
            "sso_region":                     cfg["sso-region"],
            "enforce_new_defaults":           cfg["enforce-new-defaults"],
            "reclaim_instance_interval":      cfg["reclaim-instance-interval"],
            "debug":                          cfg["debug"],
            "ssl_enabled":                    cfg["ssl-enabled"],
            "secret_key":                     self._secret_key(),
            "prometheus_endpoint":            cfg.get("prometheus-endpoint", "").strip(),
            "prometheus_enable_basic_auth":   cfg["prometheus-enable-basic-auth"],
            "prometheus_basic_auth_user":     cfg.get("prometheus-basic-auth-user", ""),
            "prometheus_basic_auth_password": cfg.get("prometheus-basic-auth-password", ""),
            "listen_port":                    cfg["listen-port"],
            "static_path":                    self._stored.static_path,
            "gunicorn_workers":               workers if workers > 0 else "__CPU_COUNT__",
            "gunicorn_timeout":               cfg["gunicorn-timeout"],
            "venv_dir":                       str(VENV_DIR),
            "skyline_conf_dir":               str(SKYLINE_CONF_DIR),
            "skyline_log_dir":                str(SKYLINE_LOG_DIR),
            "using_local_db":                 self._using_local_db(),
        }

    # ── Installation ─────────────────────────────────────────────────────────

    def _install_system_packages(self):
        self.unit.status = ops.MaintenanceStatus("Installing system packages")
        # Console build deps removed — wheel is pre-built and bundled in files/
        self._apt_install([
            "ca-certificates", "curl", "wget", "git",
            "python3", "python3-pip", "python3-venv",
            "build-essential", "make",
            "nginx", "ssl-cert",
            # client-only: used by charm-side schema checks (PK fix, leader
            # migration polling); independent of which DB backend serves data
            "mariadb-client",
        ])
        if self._using_local_db():
            self.unit.status = ops.MaintenanceStatus("Installing MariaDB")
            # Package only — the service is started in _setup_local_mariadb().
            # A freshly added unit cannot see its relations during the install
            # hook, so starting (or even enabling) MariaDB here risks racing a
            # co-located mysql-router subordinate for 127.0.0.1:3306 on units
            # that are about to join the cluster path.
            self._apt_install(["mariadb-server"])

    def _setup_venv(self):
        self.unit.status = ops.MaintenanceStatus("Creating Python virtualenv")
        self._run(["python3", "-m", "venv", str(VENV_DIR)])
        wheels_dir = self._wheels_dir()
        self._pip([
            "install", "--no-index", "--find-links", str(wheels_dir),
            "--upgrade", "pip", "setuptools", "wheel",
        ])

    def _install_apiserver(self, upgrade: bool = False):
        """
        Install skyline-apiserver from the prebuilt wheel bundled in files/wheels.
        The bundled tar.gz is only extracted to APISERVER_SRC so that the
        alembic migration tree is available for `make db_sync`.
        Fully offline: --no-index --find-links, no git, no network access.
        """
        self.unit.status = ops.MaintenanceStatus("Installing skyline-apiserver (offline bundle)")

        files_dir = Path(self.charm_dir) / "files"

        archives = sorted(files_dir.glob("skyline-apiserver-*.tar.gz"))
        if not archives:
            raise RuntimeError(
                f"No skyline-apiserver archive found in {files_dir}. "
                "Expected skyline-apiserver-*.tar.gz"
            )

        archive = archives[-1]
        logger.info("Using bundled apiserver archive: %s", archive.name)

        # extract into fixed path (needed for `make db_sync` later)
        extract_path = APISERVER_SRC
        if upgrade and extract_path.exists():
            shutil.rmtree(extract_path)
        extract_path.mkdir(parents=True, exist_ok=True)

        with tarfile.open(archive, "r:gz") as tar:
            self._safe_extract(tar, extract_path)

        # flatten a single top-level directory (e.g. skyline-apiserver-2024.2-eol/)
        # so the Makefile sits directly at APISERVER_SRC for `make db_sync`
        entries = list(extract_path.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            nested = entries[0]
            for child in nested.iterdir():
                shutil.move(str(child), str(extract_path / child.name))
            nested.rmdir()

        wheels_dir = self._wheels_dir()
        apiserver_wheels = sorted(wheels_dir.glob("skyline_apiserver-*.whl"))
        if not apiserver_wheels:
            raise RuntimeError(
                f"No skyline_apiserver wheel found in {wheels_dir}. "
                "Rebuild the offline bundle (see files/README.txt)."
            )

        wheel_path = apiserver_wheels[-1]
        logger.info("Installing apiserver wheel (offline): %s", wheel_path.name)
        self._pip([
            "install", "--no-index", "--find-links", str(wheels_dir),
            "--force-reinstall", str(wheel_path),
        ])

    def _install_console(self):
        """
        Install the pre-built skyline-console wheel bundled in the charm's
        files/ directory. No Node.js, nvm, yarn or make required at deploy time.
        """
        self.unit.status = ops.MaintenanceStatus("Installing skyline-console wheel")

        files_dir = Path(self.charm_dir) / "files"
        wheels = sorted(files_dir.glob("skyline_console-*.whl"))
        if not wheels:
            raise RuntimeError(
                f"No skyline_console wheel found in {files_dir}. "
                "Place the pre-built wheel in files/ before running charmcraft pack. "
                "See files/README.txt for build instructions."
            )

        wheel_path = wheels[-1]
        logger.info("Installing bundled console wheel: %s", wheel_path.name)
        self._pip([
            "install", "--no-index", "--find-links", str(self._wheels_dir()),
            "--force-reinstall", str(wheel_path),
        ])

        result = self._run(
            [str(VENV_PY), "-c",
             "import skyline_console, os; "
             "print(os.path.join(os.path.dirname(skyline_console.__file__), 'static'))"],
            capture=True,
        )
        self._stored.static_path = result.stdout.strip()
        logger.info("Console static path: %s", self._stored.static_path)
        self._patch_container_infra_bundle()

    def _patch_container_infra_bundle(self):
        """Patch container-infra bundle JS to fix checkVolumeQuota TypeError.

        Upstream skyline-console bug: checkVolumeQuota() destructures
        ``cinderQuota`` without a fallback. When the OpenStack deployment has
        no cinder (enableCinder=False), cinderQuota is never fetched, so the
        destructuring ``{left:l=0}=r`` throws because ``r`` is undefined.
        The error is caught by the layout's renderChildren try/catch and
        shows "Error, Unable to get Data, please go to Home page" on the
        Create Cluster page.

        Fix: ``{left:l=0}=r;`` -> ``{left:l=0}=r||{};`` (all occurrences)

        Replaces ALL occurrences because the same minified pattern appears in
        both checkInstanceQuota (harmless) and checkVolumeQuota (the actual
        bug). The V2 marker forces re-patch on files that were incorrectly
        patched by V1 (which only patched the first occurrence, hitting the
        wrong one).

        Also removes stale .gz companion files so nginx serves the patched
        .js files instead of the pre-compressed unpatched originals.

        Idempotent — the patch marker is embedded so subsequent runs are
        no-ops.
        """
        static = self._stored.static_path
        if not static:
            return
        marker = "VOL_QUOTA_PATCHED_V2"
        bad = "{left:l=0}=r;"
        good = "{left:l=0}=r||{};"
        patched = 0
        static_dir = Path(static)
        for bundle_file in static_dir.glob("container-infra.bundle.*.js"):
            try:
                text = bundle_file.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Could not read %s", bundle_file)
                continue
            if marker in text:
                logger.debug("container-infra bundle already patched (V2): %s", bundle_file.name)
                continue
            if bad not in text:
                logger.info("Patch target not found in %s (may already be fixed upstream)", bundle_file.name)
                continue
            # Strip any stale V1 marker before re-patching
            text = text.replace("// PATCHED: VOL_QUOTA_PATCHED_V1\n", "")
            count = text.count(bad)
            text = text.replace(bad, good)
            # Remove any old marker, append fresh V2 marker
            text = text.replace(f"\n// PATCHED: {marker}\n", "")
            text += f"\n// PATCHED: {marker}\n"
            bundle_file.write_text(text, encoding="utf-8")
            patched += 1
            logger.info("Patched container-infra bundle (%d occurrences): %s", count, bundle_file.name)
            # Remove stale .gz companion — nginx would serve the pre-compressed
            # unpatched version via gzip_static instead of the patched .js
            gz = bundle_file.with_suffix(bundle_file.suffix + ".gz")
            if gz.exists():
                try:
                    gz.unlink()
                    logger.info("Removed stale gzip companion: %s", gz.name)
                except Exception as exc:
                    logger.warning("Could not remove %s: %s", gz, exc)
        if patched:
            logger.info("Patched %d container-infra bundle(s) — nginx reload recommended", patched)

    # ── Database ─────────────────────────────────────────────────────────────

    def _setup_local_mariadb(self):
        self.unit.status = ops.MaintenanceStatus("Configuring local MariaDB")
        # Pin local MariaDB to 127.0.0.1:13306 (never the router's 3306) via a
        # drop-in BEFORE bringing the service up. Written on every configure:
        # idempotent, and the restart applies it to units that had started
        # with the default port earlier.
        was_active = (
            self._run(
                ["systemctl", "is-active", "--quiet", "mariadb"],
                check=False,
            ).returncode
            == 0
        )
        MARIADB_CNF_PATH.write_text(
            textwrap.dedent(
                f"""\
                # Managed by the skyline charm — do not edit.
                # Keeps local MariaDB off TCP 3306 so the co-located
                # mysql-router subordinate always owns it (3306-3309).
                [mysqld]
                bind-address = 127.0.0.1
                port = {LOCAL_MARIADB_PORT}
                """
            ),
            encoding="utf-8",
        )
        self._run(["systemctl", "enable", "mariadb"])
        if was_active:
            self._run(["systemctl", "restart", "mariadb"])
        else:
            self._run(["systemctl", "start", "mariadb"])
        db_pass = self._db_password()
        sql = textwrap.dedent(f"""\
            CREATE DATABASE IF NOT EXISTS skyline
              DEFAULT CHARACTER SET utf8
              DEFAULT COLLATE utf8_general_ci;
            GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'localhost'
              IDENTIFIED BY '{db_pass}';
            GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'%'
              IDENTIFIED BY '{db_pass}';
            FLUSH PRIVILEGES;
        """)
        # Administers over the unix socket — independent of the TCP port.
        self._run(["mysql", "-u", "root"], input_data=sql.encode())
        logger.info(
            "MariaDB local instance ready on 127.0.0.1:%s.", LOCAL_MARIADB_PORT
        )

    def _ensure_directories(self):
        for d in [SKYLINE_CONF_DIR, SKYLINE_LOG_DIR, SKYLINE_POLICY_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    def _verify_venv_deps(self, max_rounds: int = 3):
        """
        Gate: run `pip check` and force-reinstall anything reported as missing
        from the offline bundle (files/wheels) until the venv is clean.

        Every wheel in the bundle is pinned by the regenerated lockfile, so a
        reinstall is guaranteed to be deterministic. Fails hard if the venv is
        still broken after max_rounds (rather than silently shipping a broken
        install).
        """
        wheels_dir = self._wheels_dir()
        missing_re = re.compile(
            r"^\S+ [^\s,]+ requires ([^,]+), which is not installed\.$",
            re.MULTILINE,
        )
        for round_no in range(1, max_rounds + 1):
            result = self._run([str(VENV_PIP), "check"], capture=True, check=False)
            if result.returncode == 0:
                logger.info("pip check: venv dependencies clean (round %d)", round_no)
                return
            missing = {
                dep.strip() for dep in missing_re.findall(result.stdout)
            }
            if not missing:
                raise RuntimeError(
                    f"pip check failed but no missing packages were identified: "
                    f"{result.stdout.strip()}"
                )
            logger.warning(
                "pip check round %d: reinstalling missing deps from bundle: %s",
                round_no, ", ".join(sorted(missing)),
            )
            for dep in sorted(missing):
                self._pip([
                    "install", "--no-index", "--find-links", str(wheels_dir),
                    "--force-reinstall", dep,
                ])
        result = self._run([str(VENV_PIP), "check"], capture=True, check=False)
        raise RuntimeError(
            f"Dependencies still not satisfied after {max_rounds} rounds: "
            f"{result.stdout.strip()}"
        )

    def _wait_for_schema_ready(self, timeout: int = 600) -> bool:
        """
        Poll through the shared-db router until the leader's migration has
        produced an ``alembic_version`` table (cold-start multi-unit safety).
        """
        shared = self._shared_db_data()
        if not shared:
            return True
        env = self._venv_env()
        env["MYSQL_PWD"] = shared["password"]
        base = [
            "mysql", "--no-defaults",
            "-h", shared["host"],
            "-P", shared["port"],
            "-u", shared["username"],
            "-N", "-B",
        ]
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self._run(
                base
                + [
                    shared["database"],
                    "-e",
                    "SELECT version_num FROM alembic_version LIMIT 1",
                ],
                env=env,
                capture=True,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.info(
                    "leader schema detected (alembic at %s)",
                    result.stdout.strip(),
                )
                return True
            time.sleep(10)
        return False

    def _run_db_sync(self):
        self.unit.status = ops.MaintenanceStatus("Running database migration (db_sync)")
        self._verify_venv_deps()
        gated = bool(self._shared_db_data()) and not self.unit.is_leader()
        if gated:
            # Cold-start safety: on a fresh multi-unit deployment every unit
            # would race `alembic upgrade head` against the same empty schema
            # through its own router; MySQL DDL is not transactional, so all
            # but one fail with 'table already exists'. Non-leaders wait for
            # the leader's migration, then run their own as a guaranteed no-op.
            self.unit.status = ops.WaitingStatus(
                "Waiting for leader to migrate database schema"
            )
            if not self._wait_for_schema_ready():
                raise RuntimeError(
                    "leader database migration not observed within timeout; "
                    "re-run db-sync after the leader unit has finished"
                )
        self._run(["make", "db_sync"], cwd=str(APISERVER_SRC), env=self._venv_env())
        if gated:
            # Schema DDL (including the PK fix) is owned by the leader while
            # the relation is active; racing it from non-leaders could throw
            # duplicate-column errors.
            logger.info("PK fix owned by leader unit; skipping here")
        else:
            self._ensure_db_primary_keys()
        logger.info("db_sync completed successfully.")

    def _ensure_db_primary_keys(self):
        """
        Group Replication (mysql-innodb-cluster) refuses any DML on tables
        without a primary key (or non-null unique key) — MySQL error 3098.
        The stock Skyline migration 000_init.py creates `revoked_token` and
        `settings` with only a secondary index, so writes (e.g. the login
        profile's revoked-token purge) fail once the DB is a GR cluster.

        Give those tables an AUTO_INCREMENT `id` primary key, idempotently.
        Only relevant when the shared-db (router) path is active; no-op on a
        local MariaDB where schema stays exactly as Skyline ships it.
        """
        if self._using_local_db():
            return
        shared = self._shared_db_data()
        if not shared:
            return
        db = shared["database"]
        env = self._venv_env()
        env["MYSQL_PWD"] = shared["password"]
        base = [
            "mysql", "--no-defaults",
            "-h", shared["host"],
            "-P", shared["port"],
            "-u", shared["username"],
            "-N", "-B",
        ]
        for table in ("revoked_token", "settings"):
            check = [
                "SELECT COUNT(*) FROM information_schema.COLUMNS "
                f"WHERE TABLE_SCHEMA = '{db}' AND TABLE_NAME = '{table}' "
                "AND COLUMN_NAME = 'id'"
            ]
            result = self._run(
                base + [db, "-e", check[0]], env=env, capture=True, check=True
            )
            if result.stdout.strip().splitlines()[0].strip() == "1":
                logger.info("PK already present on %s — skipping", table)
                continue
            self._run(
                base
                + [
                    db,
                    "-e",
                    f"ALTER TABLE `{table}` ADD COLUMN `id` "
                    "BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST",
                ],
                env=env,
                check=True,
            )
            logger.info("Added AUTO_INCREMENT primary key to %s", table)

    # ── Configuration ─────────────────────────────────────────────────────────

    def _generate_nginx_config(self) -> bool:
        """
        Generate /etc/nginx/nginx.conf from the keystone catalog using the
        shipped `skyline-nginx-generator`.

        The generator reads /etc/skyline/skyline.yaml (rendered just before),
        queries the keystone catalog for the system user, and emits one proxy
        location per OpenStack service (/api/openstack/<region>/<service>/ ->
        the real service endpoint) plus the /api/openstack/skyline/ strip and
        /api/v1/ locations. This is how the console's overview/admin pages
        reach keystone/nova/cinder/neutron/etc.

        Our gunicorn binds 127.0.0.1:28000 (TCP) while the upstream template
        points nginx at a unix socket — the emitted config is adjusted to
        match. Returns True when the catalog-based config was written, False
        when it fell back to the static templates/nginx.conf.j2.
        """
        try:
            self.unit.status = ops.MaintenanceStatus(
                "Generating nginx config from keystone catalog"
            )
            self._run(
                [
                    str(VENV_PY),
                    "-m",
                    "skyline_apiserver.cmd.generate_nginx",
                    "--output-file", str(GENERATED_NGINX_PATH),
                    "--listen-address", f"0.0.0.0:{self.config['listen-port']}",
                    "--log-dir", str(SKYLINE_LOG_DIR),
                ],
                env=self._venv_env(),
                timeout=120,
            )
            content = GENERATED_NGINX_PATH.read_text(encoding="utf-8")
            content = content.replace(
                "server unix:/var/lib/skyline/skyline.sock fail_timeout=0;",
                "server 127.0.0.1:28000 fail_timeout=0;",
            )
            content = self._inject_health_endpoint(content)
            NGINX_CONF_PATH.write_text(content, encoding="utf-8")
            logger.info(
                "nginx.conf generated from keystone catalog -> %s", NGINX_CONF_PATH
            )
            return True
        except subprocess.TimeoutExpired as exc:
            logger.warning(
                "skyline-nginx-generator timed out after %ss; "
                "falling back to static templates/nginx.conf.j2 — "
                "OpenStack service pages will 404 until regenerated",
                exc.timeout,
            )
            self._render_template(
                "nginx.conf.j2", NGINX_CONF_PATH, self._template_context()
            )
            return False
        except Exception as exc:
            logger.warning(
                "skyline-nginx-generator failed (%s); "
                "falling back to static templates/nginx.conf.j2 — "
                "OpenStack service pages will 404 until regenerated",
                exc,
            )
            self._render_template(
                "nginx.conf.j2", NGINX_CONF_PATH, self._template_context()
            )
            return False

    def _inject_health_endpoint(self, content: str) -> str:
        """
        Add a `GET /healthz` location to the generated nginx config so load
        balancers can probe *this unit's* gunicorn liveness:

        - apiserver up   -> it 404s the unknown path -> intercepted -> 200
        - apiserver down -> nginx's own 502 passes through -> probe fails

        The database is deliberately NOT probed: it is cluster-global, so a DB
        outage takes every backend down together and per-unit removal would
        not help. Marker-guarded so repeated regenerations stay idempotent.
        """
        marker = "# skyline-charm: lb health endpoint"
        if marker in content:
            return content
        anchor = "location / {"
        block = (
            f"        {marker} (gunicorn liveness)\n"
            "        location = /healthz {\n"
            "            proxy_pass http://127.0.0.1:28000/;\n"
            "            proxy_intercept_errors on;\n"
            "            error_page 404 = @healthy;\n"
            "            access_log off;\n"
            "        }\n"
            "        location @healthy {\n"
            "            return 200 \"ok\\n\";\n"
            "        }\n\n"
        )
        if anchor not in content:
            logger.warning(
                "nginx health endpoint not injected: no %r anchor found", anchor
            )
            return content
        content = content.replace(anchor, block + anchor, 1)
        logger.info("Injected /healthz lb-health endpoint into nginx config")
        return content

    def _configure(self):
        error = self._missing_required_config()
        if error:
            self.unit.status = ops.BlockedStatus(error)
            return False
        self._patch_container_infra_bundle()
        self._publish_shared_db_request()

        if self._shared_db_related() and not self._shared_db_data():
            # Router attached but has not published credentials yet. Wait
            # instead of configuring against a half-ready (or local) database.
            self.unit.status = ops.WaitingStatus(
                "Waiting for mysql-router to publish database credentials"
            )
            logger.info("shared-db attached without credentials; deferring configure")
            return False

        self.unit.status = ops.MaintenanceStatus("Rendering configuration")
        self._ensure_directories()

        if self._using_local_db():
            self._setup_local_mariadb()
        else:
            # We are on the cluster: stop local MariaDB BEFORE db_sync so it
            # cannot keep holding 127.0.0.1:3306 away from the mysql-router
            # subordinate. check=False: mariadb may not be installed on a
            # never-local unit.
            self._run(["systemctl", "disable", "--now", "mariadb"], check=False)

        ctx = self._template_context()
        self._render_template("skyline.yaml.j2",              SKYLINE_YAML_PATH,  ctx)
        self._render_template("gunicorn.py.j2",               GUNICORN_CONF_PATH, ctx)
        self._render_template("skyline-apiserver.service.j2", SYSTEMD_UNIT_PATH,  ctx)

        if self._stored.static_path:
            generated = self._generate_nginx_config()
            logger.info(
                "nginx config source: %s",
                "keystone catalog" if generated else "static fallback",
            )
            nginx_ready = True
        else:
            logger.warning("static_path not set — nginx.conf not rendered")
            nginx_ready = False

        self._run(["systemctl", "daemon-reload"])
        self._run_db_sync()
        self._restart_services(nginx=nginx_ready)

        for rel in self.model.relations.get("website", []):
            self._publish_website(rel)

        return True

    def _restart_services(self, nginx: bool = True):
        if self._using_local_db():
            self._run(["systemctl", "enable", "--now", "mariadb"])
        else:
            # Free 127.0.0.1:3306 for the mysql-router subordinate when we've
            # flipped to the cluster-backed shared-db. check=False: mariadb
            # may not even be installed on a never-local unit.
            self._run(["systemctl", "disable", "--now", "mariadb"], check=False)
        self._run(["systemctl", "enable", "skyline-apiserver"])
        self._run(["systemctl", "restart", "skyline-apiserver"])
        if nginx:
            self._run(["nginx", "-t"])
            self._run(["systemctl", "enable", "nginx"])
            self._run(["systemctl", "reload-or-restart", "nginx"])
        self._open_listen_port()

    def _open_listen_port(self):
        """
        Open ``listen-port`` in Juju (unit firewall bookkeeping) so the port is
        listed in the `juju status` Ports column and exposed via `juju expose`
        semantics. Idempotent; closes a previously opened port if the config
        changed.
        """
        port = int(self.config["listen-port"])
        previous = int(self._stored.opened_port or 0)
        if previous and previous != port:
            try:
                self.unit.close_port("tcp", previous)
            except Exception as exc:
                logger.warning("could not close old port tcp/%s: %s", previous, exc)
        if port != previous:
            self.unit.open_port("tcp", port)
            self._stored.opened_port = port
            logger.info("Opened tcp/%s in juju", port)

    def _on_shared_db_created(self, event: ops.RelationCreatedEvent):
        """
        A mysql-router subordinate was just attached. Free 127.0.0.1:3306
        immediately — before any credentials appear — so the router's
        bootstrap never races a freshly started local MariaDB for the port
        (the failure mode that wedges scaled-out units with 'Failed to connect
        to MySQL'). Idempotent and safe when mariadb is absent.
        """
        logger.info("shared-db relation created; freeing 127.0.0.1:3306 for the router")
        self._run(["systemctl", "disable", "--now", "mariadb"], check=False)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _on_install(self, event: ops.InstallEvent):
        self.unit.status = ops.MaintenanceStatus("Starting Skyline installation")
        try:
            self._install_system_packages()
            self._setup_venv()
            self._install_apiserver()
            self._install_console()
            self._verify_venv_deps()
            self._stored.installed = True
            logger.info("Skyline software installation complete.")
            self.unit.status = ops.MaintenanceStatus("Software installed; awaiting config")
        except Exception as exc:
            logger.exception("Installation failed")
            self.unit.status = ops.BlockedStatus(f"Install failed: {exc}")

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        if not self._stored.installed:
            self.unit.status = ops.WaitingStatus("Waiting for install to complete")
            event.defer()
            return
        try:
            self._publish_secret_key()
            ok = self._configure()
            if ok:
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
        except Exception as exc:
            logger.exception("config-changed failed")
            self.unit.status = ops.BlockedStatus(f"Config error: {exc}")

    def _on_start(self, event: ops.StartEvent):
        if not self._stored.installed:
            self.unit.status = ops.WaitingStatus("Waiting for install to complete")
            event.defer()
            return
        try:
            result = self._run(
                ["systemctl", "is-active", "skyline-apiserver"], capture=True
            )
            if result.stdout.strip() == "active":
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
            else:
                self.unit.status = ops.BlockedStatus("skyline-apiserver is not active")
        except subprocess.CalledProcessError:
            self.unit.status = ops.BlockedStatus("skyline-apiserver failed to start")

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent):
        """
        Upgrade: pull latest apiserver commits and reinstall the console wheel
        from the newly packed charm (which may contain a newer wheel in files/).
        """
        self.unit.status = ops.MaintenanceStatus("Upgrading Skyline charm")
        try:
            self._install_apiserver(upgrade=True)
            self._install_console()
            self._publish_secret_key()
            ok = self._configure()
            if ok:
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
        except Exception as exc:
            logger.exception("upgrade-charm failed")
            self.unit.status = ops.BlockedStatus(f"Upgrade failed: {exc}")

    def _on_shared_db_changed(self, event: ops.RelationChangedEvent):
        """
        mysql-router (shared-db) data appeared or went away.

        When a router provides database_url it overrides the config value and
        switches the charm out of local-MariaDB mode; db_sync + templates are
        re-run so the apiserver moves to the cluster.
        """
        if not self._stored.installed:
            self.unit.status = ops.WaitingStatus("Waiting for install to complete")
            event.defer()
            return
        try:
            self._publish_secret_key()
            ok = self._configure()
            if ok:
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
        except Exception as exc:
            logger.exception("shared-db relation handler failed")
            self.unit.status = ops.BlockedStatus(f"Database relation error: {exc}")

    def _on_peers_changed(self, event: ops.RelationChangedEvent):
        """
        A leader-published secret_key (or a new peer) arrived — re-render so
        every unit signs sessions with the identical key (HA requirement).
        """
        if not self._stored.installed:
            self.unit.status = ops.WaitingStatus("Waiting for install to complete")
            event.defer()
            return
        try:
            ok = self._configure()
            if ok:
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
        except Exception as exc:
            logger.exception("peer relation handler failed")
            self.unit.status = ops.BlockedStatus(f"Peers error: {exc}")

    # ── Website relation (HAProxy backend discovery) ─────────────────────────

    def _on_website_joined(self, event: ops.RelationJoinedEvent):
        self._publish_website(event.relation)

    def _on_website_changed(self, event: ops.RelationChangedEvent):
        self._publish_website(event.relation)

    def _publish_website(self, relation: ops.Relation):
        binding = self.model.get_binding(relation)
        ingress = str(binding.network.ingress_address) if binding else ""
        port = str(self.config["listen-port"])
        relation.data[self.unit].update({
            "hostname": ingress,
            "private-address": ingress,
            "port": port,
        })
        logger.info("Published website endpoint: %s:%s", ingress, port)

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_action_db_sync(self, event: ops.ActionEvent):
        try:
            self._run_db_sync()
            self.unit.status = ops.ActiveStatus(
                f"Skyline ready on :{self.config['listen-port']}"
            )
            event.set_results({"result": "db_sync completed successfully"})
        except Exception as exc:
            event.fail(f"db_sync failed: {exc}")

    def _on_action_get_static_path(self, event: ops.ActionEvent):
        event.set_results({"static-path": self._stored.static_path or "(not yet discovered)"})

    def _on_action_restart_services(self, event: ops.ActionEvent):
        try:
            self._restart_services(nginx=bool(self._stored.static_path))
            event.set_results({"result": "Services restarted/reloaded"})
        except Exception as exc:
            event.fail(f"Restart failed: {exc}")

    def _on_action_show_config(self, event: ops.ActionEvent):
        try:
            content = (
                SKYLINE_YAML_PATH.read_text()
                if SKYLINE_YAML_PATH.exists()
                else "(not yet rendered)"
            )
            event.set_results({"skyline-yaml": content})
        except Exception as exc:
            event.fail(str(exc))

    def _on_action_regenerate_nginx(self, event: ops.ActionEvent):
        """Regenerate /etc/nginx/nginx.conf from the keystone catalog."""
        try:
            if not self._stored.installed:
                event.fail("Charm is not installed yet")
                return
            generated = self._generate_nginx_config()
            self._run(["nginx", "-t"])
            self._run(["systemctl", "reload-or-restart", "nginx"])
            self.unit.status = ops.ActiveStatus(
                f"Skyline ready on :{self.config['listen-port']}"
            )
            event.set_results({
                "source": "keystone-catalog" if generated else "static-fallback",
                "config": str(NGINX_CONF_PATH),
            })
        except Exception as exc:
            event.fail(f"regenerate-nginx failed: {exc}")

    def _on_action_patch_frontend(self, event: ops.ActionEvent):
        """Manually patch the container-infra JS bundle for cinder-less deploys."""
        try:
            if not self._stored.installed:
                event.fail("Charm is not installed yet")
                return
            static = self._stored.static_path
            if not static:
                event.fail("static_path not discovered yet")
                return
            marker = "VOL_QUOTA_PATCHED_V2"
            bad = "{left:l=0}=r;"
            good = "{left:l=0}=r||{};"
            patched = 0
            skipped = 0
            for bundle_file in Path(static).glob("container-infra.bundle.*.js"):
                text = bundle_file.read_text(encoding="utf-8")
                if marker in text:
                    skipped += 1
                    continue
                if bad not in text:
                    skipped += 1
                    continue
                text = text.replace("// PATCHED: VOL_QUOTA_PATCHED_V1\n", "")
                count = text.count(bad)
                text = text.replace(bad, good)
                text = text.replace(f"\n// PATCHED: {marker}\n", "")
                text += f"\n// PATCHED: {marker}\n"
                bundle_file.write_text(text, encoding="utf-8")
                patched += 1
                gz = bundle_file.with_suffix(bundle_file.suffix + ".gz")
                if gz.exists():
                    try:
                        gz.unlink()
                    except Exception:
                        pass
            event.set_results({
                "patched": patched,
                "skipped": skipped,
                "static-path": static,
            })
        except Exception as exc:
            event.fail(f"patch-frontend failed: {exc}")


if __name__ == "__main__":
    ops.main(SkylineCharm)
