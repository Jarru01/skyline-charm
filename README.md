# Skyline Juju Charm

Deploys **OpenStack Skyline Dashboard** (stable/2024.2) inside an LXD
container:

| Component | Detail |
|---|---|
| skyline-apiserver | Python ASGI app, gunicorn on `127.0.0.1:28000` (loopback) |
| skyline-console | Pre-built Python wheel, static assets served by nginx |
| MariaDB | Local instance, binds `127.0.0.1:13306` (optional — skipped if `database-url` is set or a `mysql-router` `shared-db` relation provides the DB) |
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

## Status / To-do

**Done & verified**

- Fully offline install from bundled wheels (apiserver + console + pinned deps)
- nginx config generated from the keystone catalog
- Databases: local MariaDB (binds `127.0.0.1:13306`, deliberately outside the
  router's 3306–3309 range); external DB via `database-url`; the HA path via a
  `mysql-router` `shared-db` → `mysql-innodb-cluster` (incl. the Group
  Replication primary-key fix — error 3098)
- Uniform session `secret_key` shared across units over `skyline-peers`
- Prometheus monitoring — set `prometheus-endpoint` and the console Monitor
  pages are populated. Each unit queries the same Prometheus API
  independently, so adding skyline units does **not** affect monitoring.
- **Multi-unit cold start:** three units deployed at once (`Step 5b`) complete
  hands-off — routers bootstrap cleanly, transient Waiting statuses appear as
  designed, exactly one unit runs the Alembic migration (leader-gated) while
  the others follow no-op, per-host grants are auto-created. The single-node
  local-DB path is separately validated end-to-end on `127.0.0.1:13306`.
- **LB health endpoint:** `GET /healthz` reflects *this unit's* apiserver
  liveness (200 up / 502 down), injected into both the generated and the
  fallback nginx configs
- Actions: `db-sync`, `show-config`, `restart-services`, `regenerate-nginx`,
  `get-static-path`

**Remaining / planned**

- **TLS termination** at the access layer (VIP currently HTTP `:80` only)

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

The charm also injects a **load-balancer health endpoint** into the server
block (generated and fallback alike): `GET /healthz` proxies a throwaway
request to gunicorn and rewrites the apiserver's inevitable 404 into **200**;
if gunicorn is down, nginx emits its own **502**. Load balancers should probe
this instead of `/` — static console files are served even when the API
backend is dead. The database is intentionally *not* part of the probe: it is
cluster-global, so an outage affects every backend identically and per-unit
removal would not help.

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

### 5a — Single unit

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm \
  --config keystone-url="https://KEYSTONE_IP:5000/v3/" \
  --config system-user-password="THE_PASSWORD_YOU_SET_ABOVE" \
  --config prometheus-endpoint="http://PROMETHEUS_IP:9090" \
  --to lxd:1
```

With no `database-url` and no router relation, the charm installs and manages
a **local MariaDB**. That instance deliberately binds **`127.0.0.1:13306`**,
*not* 3306: the co-located `mysql-router` subordinate always owns
`127.0.0.1:3306–3309`, so local DB and router can never collide regardless of
hook ordering. Attaching a `mysql-router` `shared-db` relation later stops the
local instance and moves the app to the cluster automatically.

> **`prometheus-endpoint` must include the scheme** (`http://...`). A bare
> `host:port` makes the apiserver return HTTP 500 and the Monitor pages show
> no data.

### 5b — Multiple units from scratch (HA cold start)

Prerequisite: a healthy `mysql-innodb-cluster` + vault (see
[Using an External Database](#using-an-external-database)). Deploy everything
up front and wire the relations immediately — the same order a bundle would
use:

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm skyline \
  --config keystone-url="https://KEYSTONE_IP:5000/v3/" \
  --config system-user-password="THE_PASSWORD_YOU_SET_ABOVE" \
  --config prometheus-endpoint="http://PROMETHEUS_IP:9090" \
  -n 3 --to lxd:MACHINE_A,lxd:MACHINE_A,lxd:MACHINE_B
juju deploy mysql-router skyline-mysql-router --channel 8.0/stable

juju relate skyline-mysql-router:db-router    mysql-innodb-cluster:db-router
juju relate skyline-mysql-router:certificates vault:certificates
juju relate skyline:shared-db                 skyline-mysql-router:shared-db
```

Notes:

- **`--to` is mandatory on MAAS** — without placement directives Juju asks
  MAAS for brand-new machines and hangs on *"waiting for machine"*. Give one
  directive per unit and spread them across machines for real HA.
- Relating while the units are still installing gives the cleanest ordering;
  relating later also works.
- Expected transient statuses during bring-up:
  - `Waiting for mysql-router to publish database credentials` — router still
    bootstrapping against the cluster
  - `Waiting for leader to migrate database schema` on non-leader units —
    exactly **one** unit runs the real Alembic migration, the rest follow
    with a no-op, so parallel cold starts can never race DDL

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

### Via a mysql-router backed by mysql-innodb-cluster (recommended, HA-ready)

This is the production path used by every other service in the model: a
`mysql-router` subordinate is co-located on each skyline unit and fronts a
`mysql-innodb-cluster`. The app still connects to `127.0.0.1:3306`-inside-its-
container, but that socket is now the router, which proxies to the (HA)
InnoDB Cluster — the cluster auto-provisions the `skyline` database and user.

**Prerequisite — a healthy InnoDB Cluster + vault (for TLS)**

The cluster needs to exist and be ONLINE, and vault must be able to issue the
router certificates. If you are starting from scratch:

```bash
juju deploy --channel 8.0/stable mysql-innodb-cluster --to lxd:0       # 3 units via -n 3
juju relate mysql-innodb-cluster:vault vault:certificates              # router TLS chain
# wait until: "Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure."
```

**Step 1 — Deploy the router subordinate**

```bash
juju deploy mysql-router skyline-mysql-router --channel 8.0/stable
```

**Step 2 — Wire up the relations (all three are required)**

```bash
juju relate skyline-mysql-router:db-router     mysql-innodb-cluster:db-router
juju relate skyline-mysql-router:certificates  vault:certificates
juju relate skyline:shared-db                  skyline-mysql-router:shared-db
```

Expected integrations once healthy (`juju status --relations`):

| Provider | Requirer | Interface | Purpose |
|---|---|---|---|
| `mysql-innodb-cluster:db-router` | `skyline-mysql-router:db-router` | `mysql-router` | router joins the cluster |
| `vault:certificates` | `skyline-mysql-router:certificates` | `tls-certificates` | TLS on the router ↔ cluster link |
| `skyline-mysql-router:shared-db` | `skyline:shared-db` | `mysql-shared` *(subordinate)* | DB + user provisioning |
| `skyline:skyline-peers` | `skyline:skyline-peers` | `skyline-peers` *(peer)* | uniform session secret |

**Step 3 — What happens automatically (no `database-url` config needed)**

1. The charm publishes `{database: skyline, username: skyline, hostname: <unit IP>}`
   on the requirer side of the `shared-db` relation (mysql-shared contract).
2. The router forwards that as `MRUP_*` keys to the cluster; the cluster creates
   the `skyline` database + `skyline` user and grants it access.
3. The router publishes `db_host/db_port/username/password` back. The charm
   detects it, **stops and disables the local MariaDB** (so the router can bind
   `127.0.0.1:3306`), re-renders `skyline.yaml` (`database_url` now points at
   `mysql://skyline:...@127.0.0.1:3306/skyline`) and re-runs `db_sync`.

> **Ordering is handled:** the moment the `shared-db` relation is *created* the
> charm frees `127.0.0.1:3306` and waits (`Waiting for mysql-router to publish
> database credentials`) until credentials arrive — it never starts local
> MariaDB once related, so scaled-out units never race the router's bootstrap.
> The charm also opens `listen-port` in Juju, so it appears in the
> `juju status` Ports column.

**InnoDB Cluster primary-key note (error 3098)**

Group Replication rejects *any* INSERT/UPDATE/DELETE on a table without a
PRIMARY KEY (or non-null UNIQUE key) — MySQL error 3098, *"The table does not
comply with the requirements by an external plugin."* The stock Skyline alembic
revision (`000_init.py`) creates `revoked_token` and `settings` **without**
primary keys. That is harmless on standalone MariaDB, but on the cluster it
breaks login: the profile flow's first write (a `DELETE` on `revoked_token`)
fails with 3098 and the console returns 401.

`db_sync` therefore finishes with an idempotent
`ALTER TABLE ... ADD COLUMN id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY FIRST`
on both tables (`_ensure_db_primary_keys()` in `src/charm.py`), so **no manual
DDL is needed**. Keep that step in the charm and never run a bare `alembic`
outside the charm against the cluster, or login breaks again with 3098.

**Step 4 — Verify**

```bash
juju status skyline skyline-mysql-router mysql-innodb-cluster --relations
juju run skyline show-config --wait     # database_url: mysql://skyline:...@127.0.0.1:3306/skyline
juju run skyline db-sync --wait         # migrate, including the PK fix
juju ssh skyline/0 -- 'systemctl is-active mariadb'   # expect: inactive
juju ssh skyline/0 -- 'ss -ltn | grep 330'            # expect: router on 3306-3309
```

Then log into `http://<UNIT_IP>:9999`.

### Via the `database-url` config option

Set `database-url` and the charm skips local MariaDB entirely (no install, no
`systemctl` service, no `Wants=mariadb.service`). `db_sync` and the runtime
apiserver both read the exact URL you configure.

> **Do this first.** The charm will NOT create the database, user or grants on
> an external server. Create them *before* running the config command, or the
> unit goes `blocked` when `db_sync` cannot connect. Recovery: fix the DB, then
> re-run `juju config` or `juju run skyline db-sync`.

> A `shared-db` relation, when present, **takes precedence** over
> `database-url`.

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
  previously used local MariaDB, or relating the mysql-router) leaves the local
  database and its data untouched but no longer used — no data is migrated to
  the external server. The charm runs `systemctl disable --now mariadb`, so the
  local service is stopped and disabled automatically (its package and data
  files remain on disk until you remove them manually).

---

## Scaling out / High availability

Skyline backends are **stateless** (gunicorn ASGI on `127.0.0.1:28000`, signed
session tokens, no WebSockets), so you can run several units behind a load
balancer without sticky sessions. (To stand up several units at once from
nothing, use the cold-start recipe in **Step 5b** instead.)

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm \
  --config keystone-url="https://KEYSTONE_IP:5000/v3/" \
  --config system-user-password="SKYLINE_SERVICE_PASSWORD" \
  --to lxd:1                       # NO database-url — the router drives the DB
juju relate skyline:shared-db skyline-mysql-router:shared-db
juju add-unit skyline -n 2 --to lxd:0,lxd:1   # same DB, same secret
```

> **`--to` is mandatory on MAAS.** Without a placement directive, `juju
> add-unit` asks MAAS for *brand-new machines* (and hangs forever on
> "waiting for machine" when the pool is empty). `--to lxd:0,lxd:1` puts each
> new unit into an LXD container on an existing machine — one per host, so the
> units span two physical nodes for real HA. Match the number of directives to
> `-n`.

Related **once**, scaled freely: `mysql-router` is a *subordinate*, so every
skyline unit automatically gets its own co-located router (and inherits the
cluster/vault relationships) — there is nothing to relate per unit. The
cluster provisions the `skyline` database once and creates a per-host grant
for each new unit automatically as its router checks in. The charm-side
guarantees (relation-created frees port 3306, leader-gated migrations) hold
for any unit count.

Two prerequisites are handled by the charm but worth verifying after scaling:

1. **One shared database** — every unit must use the same mysql-router
   `shared-db` relation (see above). Never scale with per-unit local MariaDB.
2. **One uniform `secret_key`** — the leader publishes it over
   `skyline-peers`; check each unit with `juju run skyline show-config --wait`.

## Access layer (Phase 2): HAProxy + Keepalived VIP

The skyline units sit behind an HAProxy access layer with a Keepalived-managed
VIP, fully Juju-configured (no manual `haproxy.cfg` edits):

```bash
# 1) haproxy application — listener + health-check policy via charm config.
#    NOTE: the value must be valid YAML; quote every scalar containing
#    braces/spaces (an unquoted '{i}' breaks yaml.safe_load in the hook).
juju deploy haproxy --channel latest/stable -n 3 \
  --config enable_monitoring=true
juju config haproxy services='[{"service_name": "skyline", "service_host": "0.0.0.0", "service_port": 80, "service_options": ["mode http", "balance leastconn", "option httpchk GET /healthz", "http-check expect status 200", "timeout client 30s"], "server_options": "check inter 10s rise 2 fall 3"}]'

# 2) keepalived subordinate on each haproxy unit + VIP
juju deploy keepalived --channel latest/stable \
  --config virtual_ip=10.11.1.200
juju integrate keepalived:juju-info haproxy:juju-info

# 3) backends: each skyline unit publishes address+port over the website
#    relation; haproxy adds/removes them automatically on scale-out/in
juju integrate skyline:website haproxy:reverseproxy
```

Rendered topology (per unit): `:80` tcp → peer haproxy units on `:81`
(active/backup), `:81` http → `skyline_be` = all skyline units with
`httpchk GET /healthz` (`inter 10s rise 2 fall 3`). Stats on `:10000`
(localhost-only by default). The VIP must be reserved in MAAS first.

### Failover test results (T1–T5)

Measured with a client loop hitting `http://10.11.1.200/healthz` every ~0.27 s.

| Test | Scenario | Result |
|---|---|---|
| T1 | Backend outage (`systemctl stop nginx` on one skyline unit) | Marked DOWN after ~24 s (`fall 3 × inter 10s`); 9/147 requests failed during detection window; service continued on remaining 2 units |
| T2 | Backend recovery (`systemctl start nginx`) | Re-entered rotation (~20 s, `rise 2 × inter 10s`); **0** user-visible failures |
| T3 | VIP MASTER outage (`systemctl stop keepalived` on haproxy/3) | VIP failed over to haproxy/4; **~1.5 s** interruption, exactly **1** dropped request |
| T4 | Original master returns (`start keepalived`) | VIP returned to haproxy/3 (preempt); **0/96** failures during fail-back |
| T5 | Non-VIP haproxy unit outage (`stop haproxy` on haproxy/4) | Peer tier marked it DOWN; VIP unaffected; **0** failures / 80 requests served; UP again after restart |

All acceptance criteria from the reference design are met; failover time is
well under the ~9 s observed there.

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
curl -I http://127.0.0.1:9999/healthz   # 502 = gunicorn dead, 200 = alive
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
  ├─ publish uniform secret_key to skyline-peers (leader)
  ├─ shared-db related but router credentials not published yet
  │    → WaitingStatus, defer the rest of configure
  ├─ create local MariaDB db/user on 127.0.0.1:13306 (if no shared-db
  │    relation and database-url is empty; never started once related)
  ├─ render skyline.yaml, gunicorn.py, skyline-apiserver.service
  │    (database_url = shared-db relation > database-url config > local MariaDB)
  ├─ GENERATE nginx.conf from the keystone catalog + inject GET /healthz
  │    (fallback to templates/nginx.conf.j2 if the generator fails)
  ├─ systemctl daemon-reload
  ├─ make db_sync  (Alembic)
  │    ├─ cluster path, non-leader unit: wait for the leader's schema first
  │    └─ leader then adds InnoDB Cluster primary keys on revoked_token /
  │         settings (idempotent ALTER; Group Replication requirement, err 3098)
  └─ enable + restart skyline-apiserver; nginx reload-or-restart;
     open listen-port in juju (Ports column)

shared-db relation-created
  └─ stop/disable local MariaDB immediately (router owns 127.0.0.1:3306)

shared-db relation-changed/broken
  └─ same re-render + db_sync path (switch to/from the router-provided DB)

skyline-peers relation-changed (new unit / rotated secret_key)
  └─ re-render with the leader-published key

start
  └─ confirm skyline-apiserver is active → set ActiveStatus
```

### Secret key persistence & HA

Sessions are signed with `secret_key`, so it must be **identical on every
unit**. The leader generates one key once and publishes it over the
`skyline-peers` relation; scaled-out units render the same value automatically.
An explicit `secret-key` config value always wins (seed or rotation) and is
propagated to all units:

```bash
juju config skyline secret-key=NEW_VALUE   # rotate — invalidates all sessions
```

Verify uniformity across units with `juju run skyline show-config --wait` on
each one.
