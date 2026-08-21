"""Fixtures shared by the tests that need something the checkout cannot supply
on its own - a live Postgres, or the nix-built language runtimes.

These tests **drop every table** in the database they are pointed at, which is
why the URL here comes from the devShell's PGUSER/PGDATABASE/PGHOST and
deliberately *not* from `load_config`: a deployment's `cocco.toml` names that
deployment's database, and `uv run pytest -m integration` must have no way of
finding it. The empty URL is the same one `DEFAULT_DATABASE_URL` is, so libpq
resolves it exactly as the app does - which is the point, without the reach.
"""

import os
from datetime import datetime, timezone

import pytest

# A challenge that has been open to the class since long before any test ran.
#
# It is here, in the one module every test file already imports implicitly,
# because `insert_challenge` - and a bare `insert(Challenge)` - leaves
# `visible_from` NULL, which means *hidden*: a fixture that did not say this
# would build a challenge no student could open, and every test about something
# else would fail with a 404 that had nothing to do with what it was testing.
#
# A fixed instant rather than `datetime.now()`, and one far from now, because
# the route tests run on SQLite: its `CURRENT_TIMESTAMP` is a naive UTC string
# and SQLAlchemy stores an aware datetime as its own wall clock, so a comparison
# between two moments seconds apart could land either way. Years apart, it
# cannot.
PUBLISHED = datetime(2020, 1, 1, tzinfo=timezone.utc)

# Whether any test actually asked for Postgres this session. The teardown below
# turns on it, because "PGDATABASE is set" is true for a plain
# `pytest -m "not integration"` inside the devShell - and wiping the developer's
# database on a run that never touched it would be a nasty surprise.
_postgres_was_used = False


# The language runtimes, built and rooted once for the whole session. Every
# integration test in `test_judge.py` reaches `resolve_runtime` without going
# through `cocco-judge`'s `main()`, and that function is a pure cache lookup
# now - so something has to fill the cache, and one fixture here is that
# something rather than a line repeated at the top of fifteen tests.
_runtimes_prewarmed = False


@pytest.fixture(autouse=True)
def _language_runtimes(request):
    """Build and root the runtimes, for the integration tests and nobody else.

    Autouse and function-scoped, but it returns at once unless the test carries
    the `integration` marker: `pytest -m "not integration"` has to go on running
    outside the dev shell with no nix and no isolate, and a session-scoped
    fixture cannot see a per-test marker to decide that.

    The import is deferred for the reason the teardown below defers its own: a
    run that never asks for any of this should not pay to import it.
    """
    if request.node.get_closest_marker("integration") is None:
        return
    global _runtimes_prewarmed
    if _runtimes_prewarmed:
        return
    import asyncio

    import cocco.judge as cj

    asyncio.run(cj.prewarm_runtimes(cj.load_languages()))
    _runtimes_prewarmed = True


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
