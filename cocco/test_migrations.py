"""The test that stops `model.py` and `cocco/migrations/versions/` drifting apart.

The rest of the suite now builds its schema by running these very revisions, so
a column added to `model.py` and to no revision would show up there as well - as
a query against a column that is not there. This file is still the direct
question rather than the inference: `compare_metadata` names the drift, on
exactly the terms `revision --autogenerate` would, instead of leaving somebody to
work it out from a failure three files away.

It runs against **`cocco-test-migrations`, a database of its own**, and that is
load-bearing rather than tidy. This test drops the whole `public` schema before
*and* after: before, because a previous run may have left an `alembic_version`
behind, and an `upgrade head` against a stale one is a silent no-op that would
make this pass on an empty database; after, so that nothing inherits a version
table pointing at head. The session database next door is migrated once and
truncated between tests, so a file that empties it would strand whatever ran
next - and which test that was would depend on collection order.
"""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from cocco.migrate import (
    COMPARE_OPTIONS,
    MIGRATIONS_DIR,
    alembic_config,
    applied_revisions,
    head_revisions,
    reset_schema,
    upgrade_to_head,
)
from cocco.model import metadata


def test_the_revisions_ship_with_the_package():
    """`script_location` is resolved from `__file__`, so this is what catches a
    wheel that shipped `cocco/migrate.py` and an empty `versions/` - an install
    that would refuse to start with "this database has no schema at all" and be
    unfixable, since `upgrade head` would have nothing to run."""
    assert MIGRATIONS_DIR.is_dir()
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert head_revisions(), "no revision scripts in cocco/migrations/versions/"


def test_the_migrations_build_exactly_the_model(migrations_database):
    engine = create_engine(migrations_database)
    try:
        with engine.begin() as conn:
            reset_schema(conn)
        with engine.begin() as conn:
            upgrade_to_head(conn)
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts=dict(COMPARE_OPTIONS))
            difference = compare_metadata(context, metadata)
        # `COMPARE_OPTIONS` is the same dict `env.py` hands autogenerate, so this
        # compares on exactly the terms `revision --autogenerate` would - the two
        # spellings would otherwise let this pass on a diff a revision records,
        # or fail on one it does not. It also means a changed *server default* is
        # the one drift this cannot catch: there are three such columns, and the
        # reasoning is on `COMPARE_OPTIONS` itself.
        assert difference == [], (
            "cocco/migrations/versions/ does not build cocco/model.py; write "
            'the missing revision with `cocco-migrate revision --autogenerate '
            '-m "what changed"`'
        )
    finally:
        with engine.begin() as conn:
            reset_schema(conn)
        engine.dispose()


def test_every_revision_downgrades_and_upgrades_again(migrations_database):
    """`downgrade base` then `upgrade head`, the proof CLAUDE.md asks of every
    revision that adds an ENUM type: `op.drop_table` leaves the type behind, so
    a `downgrade` that forgot it only fails on the *next* upgrade, as a type
    that already exists. The same goes for a cycle of foreign keys whose
    `downgrade` drops the tables in an order the database refuses."""
    engine = create_engine(migrations_database)
    try:
        with engine.begin() as conn:
            reset_schema(conn)
        with engine.begin() as conn:
            upgrade_to_head(conn)
        with engine.begin() as conn:
            command.downgrade(alembic_config(connection=conn), "base")
        with engine.begin() as conn:
            upgrade_to_head(conn)
        with engine.connect() as conn:
            assert applied_revisions(conn) == head_revisions()
            context = MigrationContext.configure(conn, opts=dict(COMPARE_OPTIONS))
            assert compare_metadata(context, metadata) == []
    finally:
        with engine.begin() as conn:
            reset_schema(conn)
        engine.dispose()
