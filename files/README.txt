This directory holds the bundled artifacts packed into the charm.

- wheels/                      the complete offline wheel bundle:
  - skyline_apiserver-*.whl    prebuilt skyline-apiserver wheel (built with
    PBR_VERSION=2024.2 from the bundled tar.gz source)
  - pip/setuptools/wheel + every runtime dependency pinned in
    wheels/requirements.lock
  The charm installs the whole venv with --no-index --find-links wheels/
  (fully offline).
- skyline_console-*.whl        the pre-built skyline-console wheel (lives at
  files/ top level, NOT inside wheels/; the charm installs it with
  --force-reinstall using wheels/ as the find-links source)
- skyline-apiserver-*.tar.gz   the skyline-apiserver source archive
  (extracted by the charm to /opt/skyline-apiserver-src so the alembic
  migration tree is available for `make db_sync`)

Regenerating the wheel bundle
-----------------------------
1. Validate a reference venv by building/installing skyline-apiserver from
   the bundled tarball and running `make db_sync`.
2. `pip freeze` the venv into wheels/requirements.lock, build the apiserver
   wheel from the tarball source (`PBR_VERSION=2024.2 pip wheel --no-deps .`)
   and `pip download --no-deps` every other locked package, plus
   pip/setuptools/wheel, into wheels/.
3. Prove it offline: a fresh venv with PIP_NO_INDEX=1 installs cleanly,
   `pip check` passes and `make db_sync` is idempotent.

See the main README.md for build instructions.
