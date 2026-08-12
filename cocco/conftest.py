"""Fixtures shared by the tests that need a live Postgres.

These tests **drop every table** in the database they are pointed at, which is
why the URL here comes from the devShell's PGUSER/PGDATABASE/PGHOST and
deliberately *not* from `load_config`: a deployment's `cocco.toml` names that
deployment's database, and `uv run pytest -m integration` must have no way of
finding it. The empty URL is the same one `DEFAULT_DATABASE_URL` is, so libpq
resolves it exactly as the app does - which is the point, without the reach.
"""

import os

import pytest


@pytest.fixture(scope="session")
def postgres_url() -> str:
    if not os.environ.get("PGDATABASE"):
        pytest.skip("no PGDATABASE: enter the Nix devShell and run `pg-start`")
    return "postgresql+psycopg://"
