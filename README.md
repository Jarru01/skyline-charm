# Skyline Juju Charm

Deploys **OpenStack Skyline Dashboard** (stable/2024.2) inside an LXD
container:

| Component | Detail |
|---|---|
| skyline-apiserver | Python ASGI app, gunicorn on `127.0.0.1:28000` (loopback) |
| skyline-console | Pre-built Python wheel, static assets served by nginx |
| MariaDB | Local instance (optional — skipped if `database-url` is set) |
| nginx | Public listener, default port `9999` |

Everything the unit needs is **bundled inside the charm** (`files/`):

- `files/skyline_console-*.whl` — pre-built console wheel
- `files/skyline-apiserver-*.tar.gz` — apiserver source (for the alembic tree)
- `files/wheels/` — a complete offline pip bundle (apiserver wheel + console
  wheel + `pip`/`setuptools`/`wheel` + every runtime dependency, all pinned)

The Python side is installed **fully offline** — no Node.js, nvm, yarn,
webpack build, git clone or pip download on the target machine. Only the OS
packages (nginx, python3-venv, mariadb, ...) are pulled via `apt-get` at
install time. The only outbound runtime dependency is OpenStack itself
(keystone, and the services the console displays), which Skyline talks to by
design.

---

## Directory Layout

```
skyline-charm/
├── charmcraft.yaml                    # Build config + charm metadata
├── config.yaml                        # All user-facing config options
├── actions.yaml                       # Juju actions
├── requirements.txt                   # Charm Python deps: ops, jinja2
├── .charmignore                       # Files excluded from the packed charm
├── .gitattributes                     # Forces LF line endings on tracked files
├── .gitignore                         # Local dev exclusions (.tmp/, *.charm, ...)
├── src/
│   └── charm.py                       # Main ops-framework charm
├── templates/
│   ├── skyline.yaml.j2                # apiserver configuration
│   ├── gunicorn.py.j2                 # gunicorn worker settings
│   ├── skyline-apiserver.service.j2   # systemd unit for gunicorn
│   └── nginx.conf.j2                  # FALLBACK nginx config (see "How it works")
├── files/
│   ├── README.txt                     # Bundle build / regeneration instructions
│   ├── skyline_console-*.whl          # ← pre-built console wheel
│   ├── skyline-apiserver-*.tar.gz     # ← apiserver source archive (db_sync)
│   └── wheels/                        # ← complete offline pip bundle
│       ├── requirements.lock          #   pinned lockfile (pip freeze)
│       ├── skyline_apiserver-*.whl    #   apiserver wheel (PBR_VERSION=2024.2)
│       └── 99 pinned wheels + lockfile>
├── .tmp/                              # dev-only scripts (git-ignored, never packed)
│   ├── build_wheels.sh                #   regenerate files/wheels
│   ├── full_proof.sh, exact_test.sh,  #   offline-install proofs
│   │   offline_proof.sh, check_db.sh
│   └── skyline-2024_2-deployment-guide.md  # upstream deployment guide
└── skyline_ubuntu-22.04-amd64.charm   # built artifact (git-ignored)
```

---

## How it works

### Routing & nginx

- nginx listens on `listen-port` (default `9999`) — the **only** public entry
  point.
- Static console assets are served by nginx directly from the installed
  `skyline_console` wheel.
- The Skyline API: `/api/openstack/skyline/*` → stripped → apiserver `/api/v1/*`.
- The console's overview/admin/monitor pages fetch OpenStack data from the
  services themselves:
  `/api/openstack/<region>/<service>/*` (keystone `v3`, nova `v2.1`,
  cinder `v3`, neutron `v2.0`, glance `v2`, ...) → proxied to the **real**
  OpenStack endpoints.
- `/api/v1/*` is also proxied straight to the apiserver for direct access.

`/etc/nginx/nginx.conf` is **generated at config time** by the shipped
`skyline-nginx-generator` (`skyline_apiserver.cmd.generate_nginx`): it reads
`/etc/skyline/skyline.yaml`, queries the keystone catalog as the skyline
system user, and emits one proxy `location` per catalogued service plus the
`/api/openstack/skyline/` and `/api/v1/` locations. The charm rewires the
generated upstream from the upstream unix socket to gunicorn's
`127.0.0.1:28000`.

`templates/nginx.conf.j2` is **only a fallback**. If the generator cannot reach
keystone at config time (or the catalog is empty), the charm renders the static
template instead: the Skyline API keeps working, but OpenStack service pages
return 404 until the config is regenerated. Fix with
`juju run skyline regenerate-nginx` (or any `juju config` change) once
keystone is reachable.

### Offline installation

The venv is installed with `pip install --no-index --find-links files/wheels`
(`PIP_NO_INDEX=1`): pip/setuptools/wheel upgrades, the apiserver wheel
(`--force-reinstall`), the console wheel, and every dependency from the pinned
bundle. A `_verify_venv_deps` gate runs `pip check` after install and before
`db_sync`, force-reinstalling anything missing from the bundle (self-healing,
fails hard if the venv is still broken after 3 rounds).

The apiserver tarball is only extracted to `/opt/skyline-apiserver-src` so the
alembic migration tree is available for `make db_sync`.

---

## Step 1 — Prepare the bundled artifacts (once, on a separate machine)

### skyline-console wheel

Build the console on Ubuntu 22.04 (Node 16 / gallium + yarn required):

```bash
apt install -y git make python3 python3-pip build-essential
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"
nvm install --lts=gallium && nvm use default
npm install -g yarn

# extract the console source, then:
cd skyline-console/
git init && git add -A && git commit -m "snapshot"
git tag 5.0.1                       # pbr uses the tag for the wheel version
make package                        # yarn install + webpack build + wheel
# dist/skyline_console-5.0.1-py3-none-any.whl
```

### skyline-apiserver archive

The apiserver is shipped as a source tarball (the charm pins `PBR_VERSION`
since the archive has no `.git`):

```bash
# extract skyline-apiserver source, then:
tar czf skyline-apiserver-2024.2.tar.gz skyline-apiserver-2024.2-eol
```

### Offline wheel bundle (files/wheels)

The complete set of wheels the unit installs from (see `files/README.txt` for
details). Regenerate it with `.tmp/build_wheels.sh` (WSL), then prove it
offline with `.tmp/full_proof.sh` (fresh venv, `PIP_NO_INDEX=1`, `pip check`
clean, `make db_sync` idempotent).

## Step 2 — Place the artifacts in the charm

```bash
cp skyline_console-5.0.1-py3-none-any.whl skyline-charm/files/
cp skyline-apiserver-2024.2.tar.gz           skyline-charm/files/
# if regenerating the bundle: replace skyline-charm/files/wheels/ wholesale
```

## Step 3 — Build the charm

```bash
cd skyline-charm/
chmod +x src/charm.py
charmcraft pack                  # add --destructive-mode when packing on a bare host
# verify the artifacts are inside:
unzip -l skyline_ubuntu-22.04-amd64.charm | grep -E 'whl|tar.gz'
```

## Step 4 — Create the OpenStack skyline service user

```bash
source /etc/kolla/admin-openrc.sh   # adjust to your openrc path

openstack user create \
  --domain admin_domain \
  --password-prompt \
  skyline

openstack role add --project admin --user skyline admin
```

## Step 5 — Deploy

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm \
  --config keystone-url="http://KEYSTONE_IP:5000/v3/" \
  --config system-user-password="THE_PASSWORD_YOU_SET_ABOVE" \
  --config prometheus-endpoint="http://PROMETHEUS_IP:9090" \
  --to lxd:1
```

> **`prometheus-endpoint` must include the scheme** (`http://...`). A bare
> `host:port` makes the apiserver return HTTP 500 and the Monitor pages show
> no data.

## Step 6 — Watch the deployment

```bash
juju status --watch 5s
```

Expected progress:
```
maintenance: Installing system packages
maintenance: Installing MariaDB
maintenance: Creating Python virtualenv
maintenance: Installing skyline-apiserver (offline bundle)
maintenance: Installing skyline-console wheel
maintenance: Software installed; awaiting config
maintenance: Rendering configuration
maintenance: Generating nginx config from keystone catalog
maintenance: Running database migration (db_sync)
active:      Skyline ready on :9999
```

## Step 7 — Access the dashboard

```bash
juju status skyline   # note the unit IP address
```

Open `http://<UNIT_IP>:9999` in a browser.

---

## Configuration Reference

| Key | Default | Description |
|---|---|---|
| `keystone-url` | *(required)* | Full Keystone v3 URL (`/v3/` is appended if missing) |
| `system-user-password` | *(required)* | Password of the `skyline` OS user |
| `database-url` | `""` | External DB URL; leave empty for local MariaDB |
| `database-password` | `""` | Local MariaDB password (auto-generated if empty) |
| `default-region` | `RegionOne` | OpenStack region |
| `system-user-name` | `skyline` | Name of the OS service user |
| `system-user-domain` | `admin_domain` | Domain of the service user |
| `system-project` | `admin` | Admin project name |
| `system-project-domain` | `admin_domain` | Domain of the admin project |
| `interface-type` | `public` | Endpoint interface (public/internal/admin) |
| `listen-port` | `9999` | nginx listener port |
| `debug` | `false` | Enable debug logging |
| `ssl-enabled` | `false` | Enable SSL flag in skyline.yaml |
| `secret-key` | `""` | Session key (auto-generated if empty) |
| `prometheus-endpoint` | `""` | Prometheus URL — **scheme required**, e.g. `http://10.0.0.3:9090`; a bare `host:port` breaks the Monitor pages |
| `prometheus-enable-basic-auth` | `false` | Basic auth when scraping Prometheus |
| `prometheus-basic-auth-user` | `""` | Prometheus Basic Auth username |
| `prometheus-basic-auth-password` | `""` | Prometheus Basic Auth password |
| `sso-enabled` | `false` | Enable SSO |
| `sso-region` | `RegionOne` | Region used for SSO |
| `enforce-new-defaults` | `false` | New RBAC defaults |
| `reclaim-instance-interval` | `604800` | Deleted instance reclaim (seconds) |
| `gunicorn-workers` | `0` | Workers (0 = auto from cpu_count) |
| `gunicorn-timeout` | `300` | gunicorn worker timeout |

---

## Actions

```bash
juju run skyline db-sync --wait
juju run skyline get-static-path --wait
juju run skyline restart-services --wait
juju run skyline show-config --wait
juju run skyline regenerate-nginx --wait   # after keystone catalog changes
```

(`skyline` targets the whole app — works whatever the unit id is. `juju run
skyline/0 ...` also works if the unit really is number 0.)

---

## Using an External Database

Set `database-url` and the charm skips local MariaDB entirely (no install, no
`systemctl` service, no `Wants=mariadb.service`). `db_sync` and the runtime
apiserver both read the exact URL you configure.

> **Do this first.** The charm will NOT create the database, user or grants on
> an external server. Create them *before* running the config command, or the
> unit goes `blocked` when `db_sync` cannot connect. Recovery: fix the DB, then
> re-run `juju config` or `juju run skyline db-sync`.

```bash
juju config skyline database-url="mysql://skyline:PASS@10.0.0.5:3306/skyline"
```

Create the database externally first:
```sql
CREATE DATABASE IF NOT EXISTS skyline
  DEFAULT CHARACTER SET utf8 DEFAULT COLLATE utf8_general_ci;
GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'%' IDENTIFIED BY 'YOUR_PASS';
FLUSH PRIVILEGES;
```

Notes:

- **Reachability**: the charm runs inside an LXD container. `localhost` /
  `127.0.0.1` in the URL means the container itself. For a DB on the Juju host
  or another machine, use its LAN IP (e.g. `10.0.0.5`), and make sure the DB
  user can connect from the container's network (`'skyline'@'%'` above).
- **MySQL 8** (as opposed to MariaDB): `GRANT ... IDENTIFIED BY` is not
  supported. Create the user separately first:
  ```sql
  CREATE USER 'skyline'@'%' IDENTIFIED BY 'YOUR_PASS';
  GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'%';
  ```
- **URL-encode special characters** in the password (`@` → `%40`, `#` → `%23`,
  `/` → `%2F`, `:` → `%3A`, `%` → `%25`), e.g.
  `mysql://skyline:p%40ss%23word@10.0.0.5:3306/skyline`.
- **Switching away from the local DB** (setting `database-url` on a unit that
  previously used local MariaDB) leaves the local database and its data
  untouched but no longer used — no data is migrated to the external server.
  The installed local MariaDB keeps running until you remove it manually.

---

## Customizing the login page image

The login page (`src/layouts/Auth/index.jsx`) uses three images installed with
the console wheel:

| Purpose | File (inside `static/asset/image/`) |
|---|---|
| Login page background (full-bleed, left side) | `login-full.<hash>.png` |
| Header logo | `logo.png` |
| Logo inside the login card | `loginRightLogo.png` |

Webpack adds a content hash to `login-full.*` (e.g.
`login-full.1786807402.png`), so **read the actual name from the unit** before
replacing it.

```bash
# 1) See the actual image filename installed on the unit
juju ssh skyline/0 -- 'sudo ls -l /opt/skyline-venv/lib/python3.10/site-packages/skyline_console/static/asset/image/ | grep -iE "login|logo"'

# 2) Copy your replacement image onto the unit
juju scp /path/to/your-background.png skyline/0:/home/ubuntu/background.png

# 3) Back up the original, then overwrite keeping the EXACT same filename
juju ssh skyline/0 -- 'sudo cp /opt/skyline-venv/lib/python3.10/site-packages/skyline_console/static/asset/image/login-full.HASH.png{,.bak} && sudo cp /home/ubuntu/background.png /opt/skyline-venv/lib/python3.10/site-packages/skyline_console/static/asset/image/login-full.HASH.png'

# 4) Reload nginx (no service restart needed), then hard-refresh the browser (Ctrl+Shift+R)
juju ssh skyline/0 -- 'sudo systemctl reload nginx'
```

Notes:
- Match the original's **dimensions** (`file` the original first).
- Replace the exact hashed filename — nginx serves by that name and the hash
  in the CSS reference must keep matching.
- The replacement is a runtime override inside the venv package. A later
  `juju refresh` re-installs the console wheel and **resets it** to the
  bundled image. For a persistent image, bundle it in the charm and overlay it
  during install.

---

## Troubleshooting

```bash
# View charm logs
juju debug-log --include unit-skyline/0 --replay

# Service logs inside the unit
juju ssh skyline/0
journalctl -u skyline-apiserver -f
systemctl status skyline-apiserver nginx mariadb
```

**Login works, but the overview/subpages/admin return 404.**
The nginx config fell back to the static template because the generator could
not reach keystone at config time. Once keystone is reachable:
`juju run skyline regenerate-nginx`. Check which config is live with:
```bash
juju ssh skyline/0 -- 'sudo grep -c "proxy_pass http" /etc/nginx/nginx.conf'
juju debug-log --include unit-skyline/0 --replay | grep -i "nginx config source"
```

**Monitor overview shows no data.**
`prometheus-endpoint` must include a scheme:
```bash
juju config skyline prometheus-endpoint="http://PROMETHEUS_IP:9090"
```
A value like `10.0.0.3:9090` makes the apiserver build an invalid URL and
return HTTP 500.

**502 Bad Gateway (nginx up, gunicorn down).**
```bash
ss -tlnp | grep 28000
journalctl -u skyline-apiserver --no-pager -n 50
```

**401 on login — skyline user missing role.**
```bash
openstack role add --project admin --user skyline admin
```

**`juju refresh --path` fails to parse the file.**
Prefix the path with `./` (a bare filename is treated as a charmstore URL):
```bash
juju refresh skyline --path ./skyline_ubuntu-22.04-amd64.charm
```

---

## Upgrading

Build the new console wheel / apiserver tarball, update the files, and refresh:

```bash
cd skyline-charm/
# replace files/skyline_console-*.whl, files/skyline-apiserver-*.tar.gz,
# and (if needed) regenerate files/wheels with .tmp/build_wheels.sh
charmcraft pack
juju refresh skyline --path ./skyline_ubuntu-22.04-amd64.charm
```

`upgrade-charm` re-installs the apiserver wheel and console wheel from the
bundle, re-extracts the tarball for `db_sync`, regenerates the nginx config and
restarts services.

---

## How the Charm Operates Internally

### Event flow on first deploy

```
install
  ├─ apt-get: baseline packages + mariadb (if local DB)
  ├─ python3 -m venv /opt/skyline-venv
  ├─ offline upgrade of pip/setuptools/wheel from files/wheels
  ├─ install apiserver wheel (--no-index --find-links --force-reinstall)
  ├─ extract bundled tarball → /opt/skyline-apiserver-src (for db_sync)
  ├─ install console wheel from files/
  ├─ discover + store console static path
  └─ verify venv deps (pip check self-heal)

config-changed  (fired automatically after install)
  ├─ validate keystone-url and system-user-password
  ├─ create local MariaDB db/user (if database-url is empty)
  ├─ render skyline.yaml, gunicorn.py, skyline-apiserver.service
  ├─ GENERATE nginx.conf from the keystone catalog
  │    (fallback to templates/nginx.conf.j2 if the generator fails)
  ├─ systemctl daemon-reload
  ├─ make db_sync  (Alembic — idempotent)
  └─ enable + restart skyline-apiserver; nginx reload-or-restart

start
  └─ confirm skyline-apiserver is active → set ActiveStatus
```

### Secret key persistence

The session `secret_key` is generated once with `secrets.token_urlsafe(32)` and
stored in `ops.StoredState`. It survives `config-changed` and `upgrade-charm`.
To rotate: `juju config skyline secret-key=NEW_VALUE` (invalidates all sessions).
