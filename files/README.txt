Place the pre-built bundled artifacts here before running charmcraft pack.

- skyline_console-*.whl          the pre-built skyline-console wheel
- skyline-apiserver-*.tar.gz     the skyline-apiserver source archive
  (installed offline by the charm with PBR_VERSION pinned by charm.py)
- typing_extensions-*.whl        typing_extensions wheel (required by
  SQLAlchemy at runtime; installed offline by the charm)

See the main README.md for build instructions.
