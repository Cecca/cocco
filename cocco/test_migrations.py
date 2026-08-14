"""The test that stops `model.py` and `cocco/migrations/versions/` drifting apart.

Everything else in the suite builds its schema with `metadata.create_all`, which
is right - a route test has no business replaying every revision - and is exactly
why this file has to exist: nothing else would ever notice a column added to
`model.py` and to no revision, and the failure that follows is a deployment that
migrates cleanly and then 500s on a missing column.

The Postgres half is destructive, and more so than the rest: it drops the whole
`public` schema of `$PGDATABASE` before *and* after. Before, because a previous
run may have left an `alembic_version` behind, and an `upgrade head` against a
stale one is a silent no-op that would make this test pass on an empty database.
After, so the next test finds an empty database rather than a version table
pointing at head. Every other integration test opens with `metadata.drop_all`,
which no-ops on an empty schema, so they are unaffected either way.
"""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from cocco.migrate import (
    COMPARE_OPTIONS,
    MIGRATIONS_DIR,
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


@pytest.mark.integration
def test_the_migrations_build_exactly_the_model(postgres_url):
    engine = create_engine(postgres_url)
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
