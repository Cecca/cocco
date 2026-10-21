---
paths:
  - "cocco/conftest.py"
  - "cocco/test_*.py"
  - "pyproject.toml"
---

# Tests

Moved from `CLAUDE.md`.

**Tests run on PostgreSQL only**, because the app does: page queries are Postgres-specific (`json_agg`, `FILTER`, `LATERAL`), FKs and `FOR UPDATE` are enforced, and `timestamptz` exists. Don't add an SQLite path.

The suite owns **`cocco-test`**, created on demand by `cocco/conftest.py`. Host, port and user come from the environment (devShell socket or `compose.yaml` both work); `PGDATABASE` and `load_config` are deliberately **not** read, because the schema is emptied between tests and must never be a developer's `cocco-dev` or a deployment's database. The schema is built once per session with `upgrade head`; each test starts from `TRUNCATE ... RESTART IDENTITY CASCADE`, so a table's first row is id 1. `test_migrations.py` uses its own `cocco-test-migrations`, because its drift test drops the schema on the way out.

`@pytest.mark.integration` means **isolate and nix** only: `isolate` with its setuid bit, `nix` with flakes, and the user allowed to run boxes (NixOS: `security.wrappers`; elsewhere install isolate outside Nix). Everything else skips, naming `pg-start`, when no server answers.
