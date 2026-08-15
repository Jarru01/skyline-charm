# WORK IN PROGRESS
# skyline Juju Charm

Deploys **OpenStack Skyline Dashboard** (stable/2024.2) including:

| Component | Detail |
|---|---|
| skyline-apiserver | Python ASGI app, gunicorn on `127.0.0.1:28000` |
| skyline-console | Pre-built Python wheel, served by nginx |
| MariaDB | Local instance (optional — skipped if `database-url` is set) |
| nginx | Public listener, default port `9999` |

The skyline-console wheel and skyline-apiserver source archive are **bundled
inside the charm** (`files/`) and installed directly. No Node.js, nvm, yarn,
webpack build, git clone or network access happens on the target machine.

---

## Directory Layout

```
skyline-charm/
├── charmcraft.yaml                    # Build config + charm metadata
├── config.yaml                        # All user-facing config options
├── actions.yaml                       # Juju actions
├── requirements.txt                   # Charm Python deps: ops, jinja2
├── .charmignore                       # Files excluded from the packed charm
├── files/
│   ├── README.txt                     # Build instructions for bundled artifacts
│   ├── skyline_console-*.whl         # ← pre-built console wheel
│   └── skyline-apiserver-*.tar.gz    # ← apiserver source archive (offline install)
├── src/
│   └── charm.py                       # Main ops-framework charm
└── templates/
    ├── skyline.yaml.j2
    ├── gunicorn.py.j2
    ├── skyline-apiserver.service.j2
    └── nginx.conf.j2
```

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

The apiserver is shipped as a source tarball and installed offline by the charm
(`PBR_VERSION` is pinned by the charm since the archive has no `.git`):

```bash
# extract skyline-apiserver source, then:
tar czf skyline-apiserver-2024.2.tar.gz skyline-apiserver-2024.2-eol
```

## Step 2 — Place the artifacts in the charm

```bash
cp skyline_console-5.0.1-py3-none-any.whl skyline-charm/files/
cp skyline-apiserver-2024.2.tar.gz           skyline-charm/files/
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
  --to lxd:1
```

## Step 6 — Watch the deployment

```bash
juju status --watch 5s
```

Expected progress:
```
maintenance: Installing system packages
maintenance: Installing MariaDB
maintenance: Creating Python virtualenv
maintenance: Installing skyline-apiserver
maintenance: Installing skyline-console wheel   ← fast, no build
maintenance: Software installed; awaiting config
maintenance: Rendering configuration
maintenance: Running database migration (db_sync)
active:      Skyline ready on :9999
```

## Step 7 — Access the dashboard

```bash
juju status skyline   # note the unit IP address
```

Open `http://<UNIT_IP>:9999` in a browser.

---

## Useful commands

```bash
# View charm logs
juju debug-log --include unit-skyline/0 --replay

# SSH into the unit
juju ssh skyline/0

# Remove the application
juju remove-application skyline --force

# Repack after changes
charmcraft clean && charmcraft pack
```

---

## Configuration Reference

| Key | Default | Description |
|---|---|---|
| `keystone-url` | *(required)* | Full Keystone v3 URL |
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
| `prometheus-endpoint` | `""` | Prometheus URL |
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
juju run-action skyline/0 db-sync --wait
juju run-action skyline/0 get-static-path --wait
juju run-action skyline/0 restart-services --wait
juju run-action skyline/0 show-config --wait
```

---

## Using an External Database

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

---

## Troubleshooting

```bash
# View charm logs
juju debug-log --include unit-skyline/0 --replay

# Service logs inside the unit
juju ssh skyline/0
journalctl -u skyline-apiserver -f
systemctl status skyline-apiserver nginx mariadb

# gunicorn not on port 28000
ss -tlnp | grep 28000
journalctl -u skyline-apiserver --no-pager -n 50

# 502 Bad Gateway (nginx up, gunicorn down)
curl -s http://127.0.0.1:28000/api/openstack/skyline/version

# 401 on login — skyline user missing role
openstack role add --project admin --user skyline admin
curl http://KEYSTONE_IP:5000/v3/
```

---

## Upgrading to a new console version

1. Build the new wheel on a separate machine
2. Replace `files/skyline_console-*.whl` with the new file
3. `charmcraft pack`
4. `juju upgrade-charm skyline --path ./skyline_ubuntu-22.04-amd64.charm`

The apiserver source is re-extracted from the bundled tarball and re-installed
on upgrade, so updating `files/skyline-apiserver-*.tar.gz` works the same way.

---

## How the Charm Operates Internally

### Event flow on first deploy

```
install
  ├─ apt-get: baseline packages + mariadb (if local DB)
  ├─ python3 -m venv /opt/skyline-venv
  ├─ extract bundled skyline-apiserver-*.tar.gz → pip install (PBR_VERSION pinned)
  └─ pip install bundled skyline_console-*.whl from files/

config-changed  (fired automatically after install)
  ├─ validate keystone-url and system-user-password
  ├─ create local MariaDB db/user (if database-url is empty)
  ├─ render skyline.yaml, gunicorn.py, skyline-apiserver.service, nginx.conf
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
