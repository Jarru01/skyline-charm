# Skyline Juju Charm

[![CI](https://github.com/Jarru01/skyline-charm/actions/workflows/ci.yaml/badge.svg)](https://github.com/Jarru01/skyline-charm/actions/workflows/ci.yaml)

Deploys **OpenStack Skyline Dashboard** (stable/2024.2) inside an LXD
container:

| Component | Detail |
|---|---|
| skyline-apiserver | Python ASGI app, gunicorn on `127.0.0.1:28000` (loopback) |
| skyline-console | Pre-built Python wheel, static assets served by nginx |
| MariaDB | Local instance, binds `127.0.0.1:13306` (optional — skipped when a `mysql-router` `shared-db` relation provides the DB) |
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
  router's 3306–3309 range); the HA path via a `mysql-router` `shared-db` →
  `mysql-innodb-cluster` (incl. the Group Replication primary-key fix —
  error 3098)
- Keystone discovery via the `identity-credentials` relation: the keystone
  charm creates the service user and supplies the public endpoint + generated
  password, taking precedence over `keystone-url` / `system-user-password`
- Uniform session `secret_key` shared across units over `skyline-peers`
- Prometheus monitoring — set `prometheus-endpoint` and the console Monitor
  pages are populated. Each unit queries the same Prometheus API
  independently, so adding skyline units does **not** affect monitoring.
- **Cold start via the identity relation (`Step 5a`):** validated end-to-end
  on a fresh app — the first unit installs offline, the keystone charm creates
  the service user and supplies the generated password, nginx is generated
  from the catalog, and the unit reaches `active` hands-off. Units added later
  re-run install and switch to the router-backed DB when their subordinate
  publishes credentials; the leader-gated migration was observed
  (`Waiting for leader to migrate database schema` on non-leaders). The
  single-node local-DB path is separately validated end-to-end on
  `127.0.0.1:13306`.
- **LB health endpoint:** `GET /healthz` reflects *this unit's* apiserver
  liveness (200 up / 502 down), injected into both the generated and the
  fallback nginx configs
- **Website relation:** every unit publishes its ingress address +
  `listen-port` on the `website` endpoint (`interface: http`, declared in
  `charmcraft.yaml`). HAProxy consumes this via its `reverseproxy` side and
  discovers/removes backends automatically — no static server lists anywhere
  (see [Access layer](#access-layer-phase-2-haproxy--keepalived-vip))
- Actions: `db-sync`, `show-config`, `restart-services`, `regenerate-nginx`,
  `get-static-path`, `patch-frontend`, `patch-kubeconfig`
- **Unit tests:** 108 tests covering helpers, nginx injection, JS patching,
  actions, lifecycle events, and relations (run with `py -3 -m pytest tests/`)

**Remaining / planned**

- **TLS termination** at the access layer (VIP serves HTTP `:80` by default;
  optional how-to: [TLS termination at the VIP](#tls-termination-at-the-vip))

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
├── tests/                             # unit tests (offline, 108 tests)
│   ├── conftest.py                    #   harness fixtures
│   ├── helpers.py                     #   shared test utilities
│   └── test_*.py                      #   action/lifecycle/relation/nginx/patch/config tests
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
`juju run skyline/0 regenerate-nginx` (or any `juju config` change) once
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

## Step 4 — Create the skyline OpenStack user (explicit-credentials path only)

Skip this step when using the recommended `identity-credentials` relation in
**Step 5a** — the keystone charm creates the user and its password
automatically.

```bash
source /etc/kolla/admin-openrc.sh   # adjust to your openrc path

openstack user create \
  --domain admin_domain \
  --password-prompt \
  skyline

openstack role add --project admin --user skyline admin

# System-scope grant: required by system-scoped admin APIs. Without it,
# some administrator panels return 403 / empty data.
openstack role add --user skyline --user-domain admin_domain --system all admin
```

> The role must be lowercase `admin`: on stock Juju deployments the role is
> created as `Admin` and has to be renamed first — oslo.policy role matching is
> case-sensitive, and with the uppercase name the Skyline admin panels stay
> read-only/empty even though login works.

## Step 5 — Deploy

### 5a — Via the `identity-credentials` relation (recommended)

Prerequisite: a healthy `mysql-innodb-cluster` + vault, and the keystone charm
(see [Database backends](#database-backends)). Deploy everything up front and
wire the relations immediately — no Keystone credentials are configured
manually:

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm skyline \
  --config prometheus-endpoint="http://PROMETHEUS_IP:9090" \
  -n 3 --to lxd:MACHINE_A,lxd:MACHINE_A,lxd:MACHINE_B
juju deploy mysql-router skyline-mysql-router --channel 8.0/stable --base ubuntu@22.04

juju integrate skyline-mysql-router:db-router    mysql-innodb-cluster:db-router
juju integrate skyline-mysql-router:certificates vault:certificates
juju integrate skyline:shared-db                 skyline-mysql-router:shared-db

juju integrate skyline:identity-credentials keystone:identity-credentials
```

The keystone charm creates (or reuses) the user named by `identity-username`
(default `skyline`) in the project/domain given by `identity-project` /
`identity-project-domain` (defaults `admin` / `admin_domain`), grants its
admin role there, and publishes the public Keystone endpoint plus the
generated password back over the relation.

Notes:

- **Keystone config is not needed.** When the relation has complete data it
  **wins** over `keystone-url`, `system-user-password`, `system-user-*`,
  `system-project*` and `default-region`; remove the relation to fall back to
  the config values.
- **Password:** generated and owned by the keystone charm; read it with
  `juju run skyline/0 show-config` (or `juju show-unit skyline/0`).
- **Existing users:** the first relation processing rewrites the user's
  password (one-time). Role grants are merged; the system-scope `admin` grant
  from Step 4 is unrelated and stays recommended for full admin panels.
- **Wrong project/domain creates a second user** — keep the defaults or set
  the options to match your cloud.
- The relation only exists on Juju-managed OpenStack clouds; use **5b**
  elsewhere.
- **`integrate` vs `relate`:** both work on Juju 3.x — `relate` is kept as a
  legacy alias of `integrate`. This guide standardizes on `integrate`.
- **`--to` is mandatory on MAAS** — without placement directives Juju asks
  MAAS for brand-new machines and hangs on *"waiting for machine"*. Give one
  directive per unit and spread them across machines for real HA.
- **`--base ubuntu@22.04` pins the mysql-router subordinate's base** to match
  the charm's (Ubuntu 22.04). The `8.0/stable` channel's newest revision
  defaults to `ubuntu@24.04`; without the pin, on a 22.04 model the
  `shared-db` relation fails with *"subordinate must support principal
  application's base"* and skyline never gets a database (login shows no
  region). If you already deployed the router and hit this, remove it
  (`juju remove-application skyline-mysql-router --force`), redeploy with the
  `--base` pin, and re-add the three `integrate` commands above.
- Expected transient statuses during bring-up:
  - `blocked: Required config 'keystone-url' is not set` — only if the first
    `config-changed` runs before the identity relation is added (e.g. you
    `juju integrate` after the app settles); clears on integrate, no config
    needed
  - `Waiting for mysql-router to publish database credentials` — router still
    bootstrapping against the cluster
  - `Waiting for Keystone credentials (identity-credentials)` — the keystone
    charm has not published credentials yet
  - `Configuring local MariaDB` — briefly on units *added* to a router-backed
    app, until their own co-located router joins (see
    [Database backends](#database-backends))
  - `Waiting for leader to migrate database schema` on non-leader units —
    exactly **one** unit runs the real Alembic migration, the rest follow
    with a no-op, so parallel cold starts can never race DDL

For a single-unit lab deployment, drop `-n 3 --to ...` and the two
`skyline-mysql-router` lines; the charm then manages a local MariaDB (see 5b).

### 5b — With explicit credentials (fallback)

Use this for non-Juju Keystone deployments, or when you manage the OpenStack
user yourself (see Step 4):

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm \
  --config keystone-url="https://KEYSTONE_IP:5000/v3/" \
  --config system-user-password="THE_PASSWORD_YOU_SET_ABOVE" \
  --config prometheus-endpoint="http://PROMETHEUS_IP:9090" \
  --to lxd:1
```

With no router relation the charm installs and manages a **local MariaDB**.
That instance deliberately binds **`127.0.0.1:13306`**, *not* 3306: the
co-located `mysql-router` subordinate always owns `127.0.0.1:3306–3309`, so
local DB and router can never collide regardless of hook ordering. Attaching
a `mysql-router` `shared-db` relation later stops the local instance and moves
the app to the cluster automatically.

> **`prometheus-endpoint` must include the scheme** (`http://...`). A bare
> `host:port` makes the apiserver return HTTP 500 and the Monitor pages show
> no data.

For multiple units, keep the explicit credentials and add the `mysql-router`
relations shown in 5a — never scale with per-unit local MariaDB.

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

## Post-deploy: new OpenStack services

The charm generates one nginx `location` block per service in the Keystone
catalog **at deploy time**. If you deploy additional services (Magnum, Heat,
Barbican, etc.) *after* Skyline, their pages will return the SPA fallback until
you regenerate the config:

```bash
juju run skyline/0 regenerate-nginx
```

Do this on every catalogue change — new services, removed endpoints, or
service-url updates.

## Configuration Reference

| Key | Default | Description |
|---|---|---|
| `keystone-url` | *(required)* | Full Keystone v3 URL (`/v3/` is appended if missing). Not needed when related to keystone via `identity-credentials` |
| `system-user-password` | *(required)* | Password of the `skyline` OS user. Not needed when related via `identity-credentials` |
| `database-password` | `""` | Local MariaDB password (auto-generated if empty) |
| `default-region` | `RegionOne` | OpenStack region |
| `system-user-name` | `skyline` | Name of the OS service user |
| `system-user-domain` | `admin_domain` | Domain of the service user |
| `system-project` | `admin` | Admin project name |
| `system-project-domain` | `admin_domain` | Domain of the admin project |
| `identity-username` | `skyline` | Username requested over `identity-credentials` |
| `identity-project` | `admin` | Project for the `identity-credentials` user |
| `identity-project-domain` | `admin_domain` | Domain for the `identity-credentials` user/project |
| `interface-type` | `public` | Endpoint interface used by the APIServer: `public`, `internal`, or `admin` (see [Endpoint interface resolution](#endpoint-interface-resolution-interface-type)) |
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

### Endpoint interface resolution (`interface-type`)

`interface-type` selects which endpoint of each service in the Keystone
catalog Skyline uses. It affects:

- the Keystone URL used for login and every server-side Keystone call
  (the identity endpoint),
- all other server-side service clients (nova, glance, cinder, neutron, ...),
- the nginx proxy routes generated from the catalog — every
  `/api/openstack/<region>/<service>/` location targets the real endpoint of
  this interface,
- the region list on the login page (regions are collected from every
  cataloged service).

Matching is an **exact interface match** (keystoneauth1) with **no fallback**:
if the cloud does not publish the configured interface for a service, that
service fails. If the *identity* service lacks it, login itself fails with 401
`Endpoint not found`; if only another service lacks it, only that service's
pages are affected. A non-empty region list does **not** prove the identity
endpoint exists — regions are aggregated across all services, so the login
call is the true test.

Check what the cloud publishes:

```bash
openstack endpoint list --service keystone -c Interface -c URL
openstack endpoint list --service container-infra -c Interface -c URL
```

When the cloud publishes all three interfaces (the common case), switching
`interface-type` is functionally transparent — only the upstream URLs change
(e.g. admin Keystone `:35357` vs public `:5000`). After a change the
config-changed hook re-renders `skyline.yaml` and regenerates nginx; force it
manually with `juju run skyline/0 regenerate-nginx`.

---

## Actions

```bash
juju run skyline/0 db-sync
juju run skyline/0 get-static-path
juju run skyline/0 restart-services
juju run skyline/0 show-config
juju run skyline/0 regenerate-nginx   # after keystone catalog changes
juju run skyline/0 patch-frontend    # fix Create Cluster page on cinder-less deploys
juju run skyline/0 patch-kubeconfig # inject kubeconfig endpoint + Download button
```

> **Actions are unit-scoped on Juju 3.6.** `juju run skyline <action>` fails
> with *"no unit specified"* — pass one or more unit ids from `juju status`
> (e.g. `juju run skyline/0 skyline/1 <action>`). The examples above use
> `skyline/0` as a placeholder. Commands that touch per-unit state
> (`show-config`, `regenerate-nginx`, `patch-*`) must be repeated on each unit
> you want to affect; `db-sync` only needs one unit (migrations are versioned,
> and a non-leader with a shared DB waits for the leader's run and no-ops).

### Frontend patches (applied automatically)

The charm automatically patches upstream Skyline Console issues at
config time (idempotent — safe to run repeatedly):

1. **`patch-frontend` / `checkVolumeQuota` TypeError (Create Cluster page):**
   Upstream `container-infra` bundle destructures `cinderQuota` without a
   fallback. When the deployment has **no Cinder** (`enableCinder=false`),
   `cinderQuota` is never fetched, so the destructuring throws
   `TypeError: Cannot destructure property 'left' of undefined`. The error
   is caught by the layout's `renderChildren` try/catch and shows
   "Error, Unable to get Data, please go to Home page". The fix replaces
   ALL occurrences of `{left:l=0}=r;` with `{left:l=0}=r||{};` in the
   minified bundle (not just the first match — there are multiple instances
   of this pattern).

2. **`checkVolumeQuota` blocks cluster creation when Cinder is absent:**
   Even after the TypeError fix, the quota check sees `left: 0` and blocks
   cluster creation when Cinder is not deployed. The Nova "Create Instance"
   flow has an `if (!this.enableCinder) return ""` guard but the Magnum
   flow is missing it. The charm injects the same guard into the minified
   `checkVolumeQuota()` so the volume check is skipped when Cinder is not
   in the service catalog.

   **Why Create Cluster Templates are unaffected:** The Template create page
   (`/container-infra/clusterTemplate/create`) only calls `getDetail()` in
   its `init()` — it never calls `getQuota()`, so the buggy
   `checkVolumeQuota()` code path is never reached.

3. **nginx generator timeout:** The keystone catalog query in the nginx
   generator now has a 120-second timeout. If the generator hangs (e.g.
   keystone unreachable), the charm falls back to the static
   `templates/nginx.conf.j2` instead of blocking the Juju hook forever.
   Previously, a hung generator would leave units stuck in
   `MaintenanceStatus("Generating nginx config from keystone catalog")`.

4. **Static-asset cache-control:** The charm injects a `location ~*` block
   into the generated nginx config with `Cache-Control: public, must-revalidate`
   (7-day expiry). This ensures browsers always revalidate static files
   with the server via ETag/Last-Modified before using a cached copy. This
   is critical because the charm patches bundle JS files in-place (same
   filename), so without `must-revalidate`, browsers would serve stale
   cached versions until the expiry elapsed.

5. **Download Kubeconfig button (`patch-kubeconfig`):** Adds a working
   "Download Kubeconfig" action to every cluster row/detail menu in the
   Magnum dashboard. Upstream Skyline calls Magnum's
   `/v1/clusters/{id}/config`, which does **not exist** in this deployment
   (404). Instead the charm:

   - Patches `main.bundle` to add a `config` extendOperation to the
     MagnumClient that `fetch()`es the apiserver (credentials included).
   - Injects a new webpack module (`9999`) into `container-infra.bundle`
     with the `DownloadKubeconfig` action (a `ConfirmAction`), wires it
     into module 1696's `moreActions`, and adds a `config()` method to the
     ClustersStore. The action's `allowedCheckFunc` enables it in **every**
     healthy completed cluster state (CREATE/UPDATE/ROLLBACK/RESUME/RESTORE/
     SNAPSHOT/ADOPT/CHECK), not just `CREATE_COMPLETE`, so the button stays
     available after a resize/update (which flips the status to
     `UPDATE_COMPLETE`); DELETE_COMPLETE and all `*_IN_PROGRESS`/`*_FAILED`
     states hide it. Already-patched bundles are upgraded in place, so a
     `juju refresh` re-applies this broadening to existing units.
   - Injects a FastAPI endpoint into `skyline_apiserver` at
     `/api/v1/clusters/{cluster_id}/kubeconfig` that authenticates via the
     session cookie/`X-Auth-Token` header, looks up the cluster + CA
     certificate from the Magnum API, generates a client key/CSR, signs it
     through Magnum's `/v1/certificates`, and returns a full
     `<cluster>-kubeconfig.yaml`. This mirrors `openstack coe cluster config`
     and produces the same cluster-admin kubeconfig the CLI exposes.

   The endpoint is a plain (non-`async`) FastAPI handler so the blocking
   keystone/Magnum HTTP calls run on worker threads, never blocking the
   apiserver event loop; the keystone URL is always read from
   `/etc/skyline/skyline.yaml` (no hardcoded fallback).

   As with the other patches it is idempotent (a stale marker is stripped and
   the file re-patched). Stale `.gz` companions are deleted after patching so
   `gzip_static` never serves the pre-compressed unpatched bundles.

   > **Known limitation:** the injected endpoint discovers the Magnum
   > (`container-infra`) endpoint with the `public` interface hardcoded,
   > independently of the `interface-type` config option. On a cloud whose
   > Magnum catalog has **no public endpoint**, "Download Kubeconfig" returns
   > HTTP 502 `container-infra endpoint not found in catalog`. Clouds that
   > publish a public Magnum endpoint (the common case) are unaffected. Check
   > with `openstack endpoint list --service container-infra -c Interface -c URL`.

6. **Network topology crash on external-only clouds:** Upstream
   `renderInstanceNode()` indexes `data.subnetNodes` blindly, but
   `renderNetworkNode()` only builds subnet nodes for **non-external**
   networks (external networks are collapsed into the single top `extNet` bar
   at the top). On a cloud with only external networks the array is empty, so
   instances whose fixed IPs fall in an external subnet pool hit
   `subnetNodes[0]` / `subnetNodes[d]` and the whole graph render aborts with
   `TypeError: e.subnetNodes[d] is undefined`. The charm guards the three
   indexing sites with fallbacks, so the `extNet` bar and the instance nodes
   still render. Clouds with internal networks are unaffected — the guards
   never trigger.

---

## Database backends

Multi-unit (HA) deployments use a `mysql-router` subordinate co-located on
each skyline unit, backed by `mysql-innodb-cluster` — the same path every
other service in the model uses (this is what **Step 5a** wires up). A single
unit with no router relation uses the charm-managed local MariaDB instead
(**Step 5b**).

### Via a mysql-router backed by mysql-innodb-cluster

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
juju integrate mysql-innodb-cluster:vault vault:certificates              # router TLS chain
# wait until: "Unit is ready: Mode: R/W, Cluster is ONLINE and can tolerate up to ONE failure."
```

**Step 1 — Deploy the router subordinate**

```bash
juju deploy mysql-router skyline-mysql-router --channel 8.0/stable --base ubuntu@22.04
```

The `--base ubuntu@22.04` pin matches the mysql-router subordinate to the
charm's base (Ubuntu 22.04); without it the newest `8.0/stable` revision
defaults to `ubuntu@24.04` and the `shared-db` relation fails with *"subordinate
must support principal application's base"*.

**Step 2 — Wire up the relations (all three are required)**

```bash
juju integrate skyline-mysql-router:db-router     mysql-innodb-cluster:db-router
juju integrate skyline-mysql-router:certificates  vault:certificates
juju integrate skyline:shared-db                  skyline-mysql-router:shared-db
```

Expected integrations once healthy (`juju status --relations`):

| Provider | Requirer | Interface | Purpose |
|---|---|---|---|
| `mysql-innodb-cluster:db-router` | `skyline-mysql-router:db-router` | `mysql-router` | router joins the cluster |
| `vault:certificates` | `skyline-mysql-router:certificates` | `tls-certificates` | TLS on the router ↔ cluster link |
| `skyline-mysql-router:shared-db` | `skyline:shared-db` | `mysql-shared` *(subordinate)* | DB + user provisioning |
| `skyline:skyline-peers` | `skyline:skyline-peers` | `skyline-peers` *(peer)* | uniform session secret |

**Step 3 — What happens automatically (no DB config needed)**

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
> database credentials`) until credentials arrive. A unit *added* later to a
> router-backed app may briefly configure against its own local MariaDB until
> its co-located router subordinate joins (observed ~4 min in a cold scale-out
> test), then stops it and switches to the cluster. Port collisions are
> impossible by design: the local instance binds `127.0.0.1:13306`, never
> 3306. The charm also opens `listen-port` in Juju, so it appears in the
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
juju run skyline/0 show-config     # database_url: mysql://skyline:...@127.0.0.1:3306/skyline
juju run skyline/0 db-sync         # migrate, including the PK fix
juju ssh skyline/0 -- 'systemctl is-active mariadb'   # expect: inactive
juju ssh skyline/0 -- 'ss -ltn | grep 330'            # expect: router on 3306-3309
```

Then log into `http://<UNIT_IP>:9999`.

---

## Scaling out / High availability

Skyline backends are **stateless** (gunicorn ASGI on `127.0.0.1:28000`, signed
session tokens, no WebSockets), so you can run several units behind a load
balancer without sticky sessions. (To stand up several units at once from
nothing, use the cold-start recipe in **Step 5a** instead.)

```bash
juju deploy ./skyline_ubuntu-22.04-amd64.charm \
  --config keystone-url="https://KEYSTONE_IP:5000/v3/" \
  --config system-user-password="SKYLINE_SERVICE_PASSWORD" \
  --to lxd:1                       # no DB config — the shared-db router drives it
juju integrate skyline:shared-db skyline-mysql-router:shared-db
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
   `skyline-peers`; check each unit with `juju run skyline/0 show-config`.

## Access layer (Phase 2): HAProxy + Keepalived VIP

The skyline units sit behind an HAProxy access layer with a Keepalived-managed
VIP, fully Juju-configured (no manual `haproxy.cfg` edits):

```bash
# 0) Reserve the VIP in MAAS first: static reservation in the same subnet /
#    VLAN as the unit addresses (here 10.11.1.200 on 10.11.0.0/16), so no
#    other machine can claim it and VRRP can move it freely.

# 1) HAProxy application — spread across machines like skyline
juju deploy haproxy --channel latest/stable -n 3 \
  --to lxd:MACHINE_A,lxd:MACHINE_A,lxd:MACHINE_B \
  --config enable_monitoring=true

# 2) Keepalived subordinate on every haproxy unit + the VIP
juju deploy keepalived --channel latest/stable \
  --config virtual_ip=10.11.1.200
juju integrate keepalived:juju-info haproxy:juju-info

# 3) Backends — each skyline unit publishes address+port over the website
#    relation; haproxy adds/removes them automatically on scale-out/in.
#    Needs a skyline charm revision that has the `website` endpoint.
juju integrate skyline:website haproxy:reverseproxy

# 4) Listener + health-check policy, set via charm config.
#    NOTE: the value must be valid YAML; quote every scalar containing
#    braces/spaces (an unquoted '{i}' breaks yaml.safe_load in the hook).
juju config haproxy services='[{"service_name": "skyline", "service_host": "0.0.0.0", "service_port": 80, "service_options": ["mode http", "balance leastconn", "option httpchk GET /healthz", "http-check expect status 200", "timeout client 30s"], "server_options": "check inter 10s rise 2 fall 3"}]'
```

> **Legacy haproxy charm quirks (`latest/stable`, rev 147):** while it has no
> backends yet (no relation / no `services` config), its hook exits with
> *"No backend servers"* and units show `hook failed: config-changed` — this
> is expected mid-setup and clears once steps 3+4 are in place. If a unit is
> stuck in `error` afterwards, `juju resolve haproxy/0` (etc.) lets the
> queued hook re-run. Also, on Juju 3.6 the initial `config-changed` may not
> fire at all after install until one of the events above triggers it — the
> same resolve clears that too.

Verify:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://10.11.1.200/healthz   # 200
curl -s -o /dev/null -w '%{http_code}\n' http://10.11.1.200/          # 200

# per-unit backend status (stats are localhost-only by default):
juju ssh haproxy/0
CREDS=$(grep 'stats auth' /etc/haproxy/haproxy.cfg | awk '{print $3}')
curl -s -u "$CREDS" 'http://127.0.0.1:10000/;csv' | grep '^skyline_be' | cut -d, -f2,18
```

Rendered topology (per unit): `:80` tcp → peer haproxy units on `:81`
(active/backup), `:81` http → `skyline_be` = all skyline units with
`httpchk GET /healthz` (`inter 10s rise 2 fall 3`). Stats on `:10000`
(localhost-only by default).

### TLS termination at the VIP

TLS is terminated at HAProxy; the skyline units keep serving plain HTTP on
`9999` and the `website` relation is unchanged — no skyline charm change is
required.

1. Obtain a certificate for a name that resolves to the VIP (preferred), or
   one with the VIP in its **IP SAN** (browsers reject CN-only-IP certs).
2. Load it into the haproxy application (base64; `SELFSIGNED` gives a
   throwaway self-signed cert):

   ```bash
   juju config haproxy ssl_cert="$(base64 -w0 fullchain.pem)" \
                      ssl_key="$(base64 -w0 privkey.pem)"
   # quick test only:
   # juju config haproxy ssl_cert=SELFSIGNED
   ```

3. Move the skyline service to `443` and attach the default certificate.
   Backends stay `<skyline-unit>:9999` plain HTTP over the internal network:

   ```bash
   juju config haproxy services='[{"service_name": "skyline", "service_host": "0.0.0.0", "service_port": 443, "crts": ["DEFAULT"], "service_options": ["mode http", "balance leastconn", "option httpchk GET /healthz", "http-check expect status 200", "timeout client 30s", "http-request add-header X-Forwarded-Proto https if { ssl_fc }"], "server_options": "check inter 10s rise 2 fall 3"}]'
   ```

4. Open TCP/443 wherever TCP/80 is allowed. `keepalived`/the VIP need no
   change — `443` binds on every haproxy unit and fails over like `80`.

Verify:

```bash
curl -sk -o /dev/null -w '%{http_code}\n' https://10.11.1.200/healthz   # 200
```

Caveats:

- The legacy haproxy charm keeps the certificate as static config; renewal
  means re-running the `juju config` commands above (or integrating the
  certbot charm for automated issuance).
- If SSO is enabled, also `juju config skyline ssl-enabled=true` so Skyline
  builds `https` origin URLs.

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
`juju run skyline/0 regenerate-nginx`. Check which config is live with:
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

**Login page shows no region (empty region dropdown).**

The dropdown is populated by the console calling
`/api/v1/contrib/regions`; that endpoint authenticates to Keystone with the
`system-user-*` credentials and reads the service catalog, so no regions means
that call failed or returned nothing.

Most common cause: the `skyline` user exists with the correct password but is
missing the project-scoped `admin` role (the system-scope `admin` grant is
needed as well for some admin panels). Verify on the cloud:

```bash
openstack role assignment list --user skyline --names
# expect: admin on project admin + admin at system scope
```

On a unit, the HTTP response body carries the exact error — the apiserver
does **not** log it, so `/var/log/skyline` stays quiet:

```bash
juju ssh skyline/0 -- 'curl -s http://127.0.0.1:28000/api/v1/contrib/regions'
```

- `{"detail": "..."}` with HTTP 401/500 → authentication or connectivity
  failure (wrong password, missing role/domain, Keystone unreachable or TLS).
- `[]` with HTTP 200 → auth works, but the catalog has no endpoints for the
  configured `interface-type` (see
  [Endpoint interface resolution](#endpoint-interface-resolution-interface-type)).

Fix the roles, then regenerate nginx and reload the login page:

```bash
openstack role add --project admin --user skyline admin
openstack role add --user skyline --user-domain admin_domain --system all admin
juju run skyline/0 regenerate-nginx
```

Which upstream URLs nginx actually uses (admin vs public vs internal):

```bash
juju ssh skyline/0 -- 'sudo grep "proxy_pass http" /etc/nginx/nginx.conf'
```

For a historical view of failing calls (the error detail itself is only in the
response body above, not in this log):

```bash
juju ssh skyline/0 -- "sudo grep ' 500 ' /var/log/nginx/skyline_access.log"
```

**`juju refresh --path` fails to parse the file.**
Prefix the path with `./` (a bare filename is treated as a charmstore URL):
```bash
juju refresh skyline --path ./skyline_ubuntu-22.04-amd64.charm
```

**Units stuck in MaintenanceStatus "Generating nginx config from keystone catalog".**
The nginx generator subprocess hung (e.g. keystone unreachable) and the Juju
hook blocked. Charm ≥ rev 57 has a 120-second timeout on the generator — it
falls back to the static template instead of blocking. To recover on older
revs, refresh the charm and the `upgrade-charm` hook re-runs `_configure()`:
```bash
juju refresh skyline --path ./skyline_ubuntu-22.04-amd64.charm
```

**Create Cluster page shows "Error, Unable to get Data, please go to Home page".**
The upstream `container-infra` bundle has a `checkVolumeQuota()` bug that
crashes when Cinder is not in the service catalog. The charm auto-patches this
at config time (V3 patch — fixes both the TypeError and the missing
`enableCinder` guard). If you see it on a pre-patch deployment:
```bash
juju run skyline/0 patch-frontend
# then hard-refresh the browser (Ctrl+Shift+R)
```
If the error persists after patching, it may be a browser cache issue. The
charm uses `must-revalidate` cache headers, but older deployments may have
`immutable` headers. Clear the browser cache manually (Ctrl+Shift+Delete)
and hard-refresh, or run `juju run skyline/0 regenerate-nginx` to
update the nginx config with the correct cache headers.

**Network → Topology is empty (console: `e.subnetNodes[d] is undefined`).**
Fixed automatically by the charm — the `network.bundle` guard patch runs at
config time (see [Frontend patches](#frontend-patches-applied-automatically)).
If you still see an empty graph, confirm the patch is on the unit and
hard-refresh the browser:

```bash
juju ssh skyline/0 -- "sudo grep -c '||{cardY:190}' /opt/skyline-venv/lib/python3.10/site-packages/skyline_console/static/network.bundle.*.js"
# expect: 1
```

Background: upstream `renderInstanceNode()` indexes `data.subnetNodes`, which
is empty when the project has only external networks (all five topology API
calls still return 200). External networks always render as the single top
`extNet` bar; instances/routers attached to them are drawn connected to that
bar. On charm revisions without the patch, create one internal network with a
subnet as a workaround.

**Login stopped working after relating keystone (`identity-credentials`).**
The keystone charm generates (and adopts) the password for the service user
the first time the relation is processed, so a previously configured
`system-user-password` no longer works. Read the current one with
`juju run skyline/0 show-config` and update any scripts/openrcs. The
relation's URL and credentials always take precedence over the config values;
remove the relation to fall back.

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
  ├─ extract bundled tarball → /opt/skyline-apiserver-src (for db_sync)
  ├─ install apiserver wheel (--no-index --find-links --force-reinstall)
  ├─ install console wheel from files/
  ├─ discover + store console static path
  └─ verify venv deps (pip check self-heal)

config-changed  (fired automatically after install)
  ├─ validate keystone-url and system-user-password (skipped when the
  │    identity-credentials relation provides credentials)
  ├─ publish uniform secret_key to skyline-peers (leader)
  ├─ identity-credentials related but no credentials published yet
  │    → WaitingStatus, defer the rest of configure
  ├─ shared-db related but router credentials not published yet
  │    → WaitingStatus, defer the rest of configure
  ├─ create local MariaDB db/user on 127.0.0.1:13306 (if no shared-db
  │    relation; never started once related)
  ├─ render skyline.yaml, gunicorn.py, skyline-apiserver.service
  │    (keystone url/user = identity-credentials relation > config;
  │     database_url = shared-db relation > local MariaDB)
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

website relation-joined/changed  (+ re-publish after every successful
│                                   configure, e.g. listen-port change)
  └─ publish {hostname, private-address, port} = this unit's ingress
       address + listen-port → HAProxy (reverseproxy) picks it up as a
       backend server with health checks

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

Verify uniformity across units by running `juju run skyline/<unit> show-config`
on each one (e.g. `skyline/0`, `skyline/1`, ...).

## Testing

108 local unit tests cover the charm's logic layer — helper functions, nginx
injection, JS bundle patching, action handlers, lifecycle events, and relation
handlers. They run entirely offline (no Juju/MAAS required) and mock all
subprocess calls.

```bash
py -3 -m pip install "ops>=2.9.0" jinja2 pytest   # one-time
py -3 -m pytest tests/ -v
```

Tests do **not** verify real filesystem paths, template rendering, or
deployed-cluster behaviour. For full confidence, deploy to a test model and
run the smoke checks from the sections above.
