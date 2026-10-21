---
paths:
  - "cocco/config.py"
  - "cocco/server.py"
  - "cocco/judge.py"
  - "cocco/migrate.py"
  - "pyproject.toml"
  - "Justfile"
  - "cocco.*.toml"
---

# Packaging and configuration

Moved from `CLAUDE.md`. The short version stays there.

**cocco is a package with four commands** (`[project.scripts]`): `cocco` (`cocco/cli.py`, the transfer CLI), `cocco-web` (`cocco/server.py`), `cocco-judge` (`cocco/judge.py:main`), `cocco-migrate` (`cocco/migrate.py`). `uv sync` installs it editable. Everything read at runtime — `templates/`, `static/`, `languages.toml`, `evaluators/`, `migrations/` — lives **inside `cocco/`** and is resolved from `Path(__file__).resolve().parent`, never the working directory, because an installed wheel has no repo root. There is deliberately **no `alembic.ini`** anywhere: plain `alembic` run from the checkout would pick it up and migrate whatever database *it* names instead of `cocco.toml`'s. `assets/` is tailwind input plus design-source icons; never read by Python, not shipped. `cocco/static/styles.css` is gitignored, so `[tool.hatch.build.targets.*] artifacts` names it explicitly, and it must be built before `uv build` or the wheel ships without a stylesheet.

`cocco-web` is a **thin uvicorn wrapper in its own module**, not a function in `main.py`: `cocco.main` reads its config at import (`CONFIG`, the engine, `Storage(...)`, `load_languages()` are module-level), so a `--config` flag parsed inside it would arrive too late. `server.py` sets `COCCO_CONFIG` first, then hands uvicorn the import string `"cocco.main:app"` — the only form `--reload` and `--workers` accept. `cocco-judge`'s `main` is module-level and sync because an entry point cannot name an `async def` under `if __name__ == "__main__"`.

The web app and the judge are **separate processes** that must agree on the database, `[storage] root`, and the *installed package* (`languages.toml` maps extensions at both ends; a judge on an older version grades with an older `equality.py`). The first two come from the same config file — the judge takes only `--config`, no `--database`/`--storage`, because a flag can disagree with the file. Submitting only enqueues a row; nothing leaves `queued` without a judge on the same database.

`[dev] reset_db` drops the schema and reseeds dummy data on startup. Keep it false (as `cocco.dev.toml` ships it): `--reload` would wipe the data on every file change. It discards *data*; schema changes are migrations.

**Config: the file is settings, the environment is secrets.** `cocco/config.py` reads both (its own module because the judge reads it too). The file carries `[database] url`, `[storage] root`, `[judge] flake`, `[session]`, `[auth]`, `[site]`, `[dev]`. `COCCO_SECRET_KEY`, `COCCO_GOOGLE_CLIENT_ID`/`_SECRET`, `COCCO_GITHUB_CLIENT_ID`/`_SECRET` have **no file spelling**, so there is no precedence and nothing secret in a copied file. `COCCO_CONFIG` names the file, so it is the one setting that can't live in it.

`cocco.dev.toml` is **committed** (secret-free; the Justfile points at it); `cocco.toml` is gitignored, templated by `cocco.example.toml`. **A missing file is not an error**: `DEFAULT_DATABASE_URL` is the empty `postgresql+psycopg://`, which libpq resolves from `PGUSER`/`PGDATABASE`/`PGHOST` — that keeps the devShell's `$PWD/.pg/sock` out of git and lets `cocco.main` import with no environment. Settings in their old place (`COCCO_DUMMY_AUTH`, a file `secret_key`) are **warned about, not fatal**, by `_warn_about_removed_settings`; `dummy_user` was deleted outright (`/login` asks instead). `[dev] dummy_auth` makes a fresh checkout usable; `[auth] administrators` bootstraps a deployment's first administrators.

- **A secret has one source and never a fixed default.** Secrets come from the environment only (see Commands). With `COCCO_SECRET_KEY` unset, `load_config` generates a random per-process key and warns: loud (logouts on restart, workers disagree) rather than a silent signing key shipped in the code.
