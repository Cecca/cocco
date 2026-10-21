"""Fixtures shared by the tests that need something the checkout cannot supply
on its own - a live PostgreSQL, or the nix-built language runtimes.

**The tests run on PostgreSQL and nothing else**, because the app does. There is
no SQLite route here any more: `repository.py`'s page queries are built on
Postgres - `json_agg`, `FILTER`, `LATERAL`, `DISTINCT ON` - and cannot execute
anywhere else, foreign keys and `FOR UPDATE` are enforced by one of the two and
silently ignored by the other, and `timestamptz` is a type only one of them has.
A test that passed on the other database was not evidence about this one.

The database is **`cocco-test`, and the tests own it**. Host, port and user come
out of the environment - so the devShell's socket and the compose container both
work with no configuration - but `PGDATABASE` is deliberately *not* read: that
names a developer's own `cocco-dev` data, and a deployment's `cocco.toml` names
something worse. `load_config` is not read either, for the same reason and more
so. The tests empty this database between every test; it must therefore be one
nobody minds losing.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from cocco.migrate import reset_schema, upgrade_to_head
from cocco.model import metadata

# The scratch database, and the one beside it that `test_migrations.py` is
# allowed to destroy. The second exists because that file's drift test ends with
# `reset_schema` in a `finally`, leaving *no* tables and no `alembic_version`:
# sharing the session database with it would leave whatever ran next truncating
# relations that no longer exist, and which test that was would depend on
# collection order.
#
# One database, truncated between tests, so the suite is **serial**. There is no
# pytest-xdist here; whoever reaches for `-n auto` wants a database per worker,
# which is these two names suffixed with `PYTEST_XDIST_WORKER` and `upgrade head`
# paid once per worker - not a shared one with the races that would follow.
TEST_DATABASE = "cocco-test"
MIGRATIONS_DATABASE = "cocco-test-migrations"


def _url(database: str) -> str:
    """A URL that names its database and resolves everything else from libpq.

    The path is spelled out and the authority is left empty, so `PGHOST` -
    which in the devShell is a unix socket path, slashes and all - still
    resolves, while `PGDATABASE` is overridden rather than obeyed.
    """
    return f"postgresql+psycopg:///{database}"


def _create_database(name: str) -> None:
    """Create `name` if it is not there. Skips the test if nothing answers.

    `CREATE DATABASE` cannot run inside a transaction, hence AUTOCOMMIT, and it
    runs against `postgres` because a database cannot be created from itself.
    """
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            existing = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            ).first()
            if existing is None:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        pytest.skip(
            f"no PostgreSQL to run against: enter the Nix devShell and run "
            f"`pg-start` ({exc.orig or exc})"
        )
    finally:
        admin.dispose()


# A challenge that has been open to the class since long before any test ran.
#
# It is here, in the one module every test file already imports implicitly,
# because `insert_challenge` - and a bare `insert(Challenge)` - leaves
# `visible_from` NULL, which means *hidden*: a fixture that did not say this
# would build a challenge no student could open, and every test about something
# else would fail with a 404 that had nothing to do with what it was testing.
#
# A fixed instant rather than `datetime.now()` so that a test reads the same
# thing on every run, and one comfortably in the past so that no clock skew
# between this process and the database's `now()` can make it a close call.
PUBLISHED = datetime(2020, 1, 1, tzinfo=timezone.utc)


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

    The marker now means isolate and nix, and only those - the database tests
    gave it up when they stopped being the exception - so this no longer runs
    `nix build` three times on the way to a query.
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


########################################################################
# the database


# Every table the model owns, for the per-test reset below. `alembic_version` is
# not in `metadata` and must survive it: the schema is migrated once, and a
# database that forgot which revision it was at would be migrated again.
_ALL_TABLES = ", ".join(f'"{table.name}"' for table in metadata.sorted_tables)


@pytest.fixture(scope="session")
def database():
    """The scratch database, created on demand and brought to head.

    Built with `cocco-migrate upgrade head` and not `metadata.create_all`,
    which is the whole point of running on the real thing: what the tests are
    given is the DDL a deployment gets, server defaults and ENUM types
    included. It costs a third of a second, once.
    """
    _create_database(TEST_DATABASE)
    engine = create_engine(_url(TEST_DATABASE))
    try:
        # Two transactions, and in `prepare_database`'s order: the drop has to
        # be committed before alembic starts its own work on the connection.
        with engine.begin() as conn:
            reset_schema(conn)
        with engine.begin() as conn:
            upgrade_to_head(conn)
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def migrations_database() -> str:
    """A database `test_migrations.py` may leave in any state it likes.

    Its own, precisely because that file's drift test drops the schema on the
    way out and the session schema above is built once.
    """
    _create_database(MIGRATIONS_DATABASE)
    return _url(MIGRATIONS_DATABASE)


@pytest.fixture
def db(database):
    """The session database, emptied for this test. A sync engine.

    `TRUNCATE` rather than dropping and recreating the schema: nine
    milliseconds against eighty, and `RESTART IDENTITY` is what keeps the first
    row of every table at id 1, which more than one test names out loud.
    `CASCADE` is what lets one statement do it in spite of the foreign keys -
    which, unlike before, are real.
    """
    with database.begin() as conn:
        # `TRUNCATE` wants ACCESS EXCLUSIVE on every table and will queue
        # behind any reader, so one connection leaked by an earlier test would
        # hang here with nothing on the terminal to say why. Five seconds turns
        # that into a failure that names the table it was waiting for.
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE"))
    return database


@pytest.fixture
async def app_engine(db):
    """The async engine the app is pointed at, over the same emptied database.

    Per test rather than per session, which costs about eleven milliseconds and
    keeps the teardown honest. It is safe to share one across tests - psycopg's
    async connections are not bound to an event loop, unlike asyncpg's, so a
    pooled connection survives both pytest-asyncio's per-test loop and the
    separate loop `TestClient` runs the app in - and that is the change to make
    if those milliseconds ever matter.
    """
    engine = create_async_engine(_url(TEST_DATABASE))
    yield engine
    await engine.dispose()


########################################################################
# the app


def sign_in_as(client, email: str):
    """Become somebody else.

    Dummy auth is a real session now, so this is a request rather than a
    monkeypatch, and `_sign_in` clears the old session before writing the new
    id - no identity leaks into the next one.
    """
    response = client.post(
        "/auth/dummy", data={"email": email}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


@pytest.fixture
def make_client(app_engine, monkeypatch):
    """Build a `TestClient` whose app talks to the test database.

    A factory rather than a fixture, because the files that want one differ in
    exactly two things: who is signed in, and whether the development sign-in
    box is on at all. Everything else - the `get_db` override, the deliberate
    absence of a context manager - was written out seven times before.

    Deliberately not `with TestClient(...)`: entering the context runs the
    startup event, which calls `prepare_database` against the module-level
    engine rather than this one.
    """
    import cocco.main as main
    from cocco.web import core

    def _make(*, as_: str | None = None, dummy_auth: bool = True):
        async def _get_db():
            async with app_engine.connect() as conn:
                yield conn

        main.app.dependency_overrides[core.get_db] = _get_db
        monkeypatch.setattr(core, "DUMMY_AUTH", dummy_auth)
        client = TestClient(main.app)
        if as_ is not None:
            sign_in_as(client, as_)
        return client

    yield _make
    main.app.dependency_overrides.clear()
