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

# Whether any test actually asked for Postgres this session. The teardown below
# turns on it, because "PGDATABASE is set" is true for a plain
# `pytest -m "not integration"` inside the devShell - and wiping the developer's
# database on a run that never touched it would be a nasty surprise.
_postgres_was_used = False


@pytest.fixture(scope="session")
def postgres_url() -> str:
    if not os.environ.get("PGDATABASE"):
        pytest.skip("no PGDATABASE: enter the Nix devShell and run `pg-start`")
    global _postgres_was_used
    _postgres_was_used = True
    return "postgresql+psycopg://"


@pytest.fixture(scope="session", autouse=True)
def _leave_the_database_empty():
    """Drop the schema once the Postgres tests are done.

    Not tidiness - it is what keeps `just dev` working after a test run. These
    tests build their schema with `metadata.create_all`, which writes no
    `alembic_version`, so whichever of them ran last would otherwise leave a
    database full of tables that alembic believes is at base: the next
    `cocco-migrate upgrade head` then tries to create a table that is already
    there and fails, and the developer is left with a stack that will not start
    and a traceback that does not say why. An empty schema is a state
    `upgrade head` can always act on.
    """
    yield
    if not _postgres_was_used:
        return
    from sqlalchemy import create_engine, text

    engine = create_engine("postgresql+psycopg://")
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
