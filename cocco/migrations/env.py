"""How alembic reaches this deployment's database.

Two departures from the stock template, and they are the same departure twice.
There is no ini file, so:

* the URL does not come from `config.get_main_option("sqlalchemy.url")`. It
  comes from `load_config(config_path())` - the same call `cocco.main` and
  `cocco.judge` make, so `cocco-migrate` cannot migrate a database the two
  server processes are not talking to. It is also the only way the empty
  `postgresql+psycopg://` survives at all: written into an ini file it would be
  a configparser value, and a `%` in a password would make one unreadable.
* `fileConfig(config.config_file_name)` is gone. `config_file_name` is None
  here and the call would raise. `cocco-migrate` runs `logging.basicConfig`
  instead.

A plain sync engine, and none of alembic's async template: psycopg is the
driver precisely because it has both faces (see `DEFAULT_DATABASE_URL`), and a
migration has no reason to want the async one.

**Two things autogenerate cannot see here.** The first is a changed *server*
default - `compare_server_default` is off, and `COMPARE_OPTIONS` in
`cocco/migrate.py` says why; there are three such columns and they are written
by hand. The second is a **new value on an existing enum**: adding a member to
`OutcomeEnum` shows up as no diff whatsoever, and needs a hand-written revision
containing `op.execute("ALTER TYPE outcomeenum ADD VALUE 'something'")` wrapped
in `with op.get_context().autocommit_block():`, because a value added inside a
transaction cannot be used until that transaction commits.
"""

from alembic import context
from sqlalchemy import create_engine, pool

from cocco.config import config_path, load_config
from cocco.migrate import COMPARE_OPTIONS
from cocco.model import metadata

config = context.config

# What `--autogenerate` diffs the database against. `model.py` is the head of
# the sequence of revisions, and `cocco/test_migrations.py` is what keeps that
# true.
target_metadata = metadata


def _url() -> str:
    """The database to migrate.

    `attributes` is a plain dict, unlike the ini machinery it sits beside - it
    is how a test hands in the scratch database it is allowed to destroy. With
    nothing there, this is the same file every other cocco process reads.
    """
    return config.attributes.get("url") or load_config(config_path()).database.url


def _run_migrations(connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, **COMPARE_OPTIONS
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """`--sql`: emit the DDL for somebody else to apply, connecting to nothing."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMPARE_OPTIONS,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        # Somebody else owns the transaction: `[dev] reset_db` and the drift
        # test both run migrations on a connection they are already holding,
        # rather than opening a second one to the same database.
        _run_migrations(connection)
        return

    engine = create_engine(_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as own_connection:
            _run_migrations(own_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
