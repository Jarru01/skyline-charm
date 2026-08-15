Place the pre-built bundled artifacts here before running charmcraft pack.

- wheels/                      the complete offline wheel bundle:
  - skyline_apiserver-*.whl    prebuilt skyline-apiserver wheel (built with
    PBR_VERSION=2024.2 from the bundled tar.gz source)
  - skyline_console-*.whl      the pre-built skyline-console wheel
  - pip/setuptools/wheel + every runtime dependency pinned in
    wheels/requirements.lock
  The charm installs the whole venv with --no-index --find-links wheels/
  (fully offline; see the regeneration script .tmp/build_wheels.sh in the repo).
- skyline-apiserver-*.tar.gz   the skyline-apiserver source archive
  (extracted by the charm to /opt/skyline-apiserver-src so the alembic
  migration tree is available for `make db_sync`)

Regenerating the wheel bundle
-----------------------------
1. Validate a reference venv (/opt/skyline-venv) by building/installing
   skyline-apiserver from the bundled tarball and running `make db_sync`.
2. `.tmp/build_wheels.sh` (WSL) freezes the venv, builds the apiserver wheel
   and downloads every dependency as a wheel into wheels/.
3. Prove it offline with `.tmp/full_proof.sh`: fresh venv, PIP_NO_INDEX=1,
   `pip check` clean, `make db_sync` idempotent.

See the main README.md for build instructions.
