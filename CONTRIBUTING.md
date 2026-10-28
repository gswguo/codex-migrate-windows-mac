# Contributing

Use Python 3.11 or newer. The core runtime and synthetic tests use only the Python standard library.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m codex_migrate --help
```

In PowerShell, set `$env:PYTHONPATH = 'src'`, then use `python` for the commands above.

Write a failing synthetic regression before changing behavior. Never run mutation tests against your real Codex home. Create disposable homes, database rows, project files, and identifiers in temporary directories. Do not commit any personal migration packages, conversation contents, UUIDs, filesystem inventories, or credentials. Public fixtures must be invented and reproducible.

For a native adapter change, document the exact schema, byte-offset semantics, item fields, and app-server capabilities. Preserve unknown user data and fail closed when native compatibility cannot be proven. Tests on macOS or Windows runners do not establish desktop acceptance on either platform.

Pull requests should explain the concrete problem, changed behavior, and verification performed. Report failures and untested platform behavior explicitly. Review [the design](docs/superpowers/specs/2026-10-27-cross-platform-migration-design.md), [the implementation contract](docs/implementation-contract.md), and [security guidance](SECURITY.md) before modifying import or recovery code.

Contributions are provided under the repository's [MIT license](LICENSE).
