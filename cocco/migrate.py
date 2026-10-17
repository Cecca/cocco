"""The schema, as a sequence of revisions - and the one command that moves it.

Both halves in one module, the way `judge.py` is both the judge and
`cocco-judge`: "what revision is this database at" is a question three
processes ask, so it is answered in one place, and the command that changes
the answer belongs next to it. Not `cocco/schema.py`, which is taken - those
are the Pydantic mirrors - and not `cocco/migrations.py`, which would collide
with the `cocco/migrations/` directory.

The revision scripts live at `cocco/migrations/`, **inside the package** and
resolved from `__file__`, for the reason every other runtime file in this repo
is (`templates/`, `static/`, `languages.toml`, `evaluators/`): an installed
`cocco-migrate` has no repository root to be relative to. There is deliberately
**no `alembic.ini`** anywhere - the working directory configures nothing here,
and a stray ini would be found by anyone running plain `alembic` from the
checkout, who would then migrate whatever database *it* named rather than the
one `cocco.toml` does.

The schema is not created on boot any more. `cocco-web` and `cocco-judge` ask
`check_schema_is_current` and refuse to start against a database that is
behind, printing the command that fixes it - loud, and never DDL nobody asked
for.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

# Resolved against the package, never the working directory - see the module
# docstring. This is the one thing `alembic_config` has to write down.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Spelled once, read by `migrations/env.py` and by `test_migrations.py`, so that
# the drift test compares on exactly the terms autogenerate writes on. If these
# two disagreed, the test would either pass on a diff a revision would record or
# fail on one it would not.
#
# `compare_server_default` is off on purpose, and it is not laziness: Postgres
# reads `challenge.ranking`'s default back as `'running_time'::leaderboardranking`
# against a model that says `text("'running_time'")`, and a SERIAL primary key
# reflects a `nextval(...)` the model does not declare. Both would be permanent
# phantom drift. The cost is that a *changed* server default is the one thing
# autogenerate will not notice; there are three such columns in the whole schema
# (`challenge.ranking`, `challenge.position`, `testcase.is_active`) and they are
# written by hand.
#
# Annotated `dict[str, Any]` rather than left to infer as `dict[str, bool]`, so
# that `**COMPARE_OPTIONS` type-checks against `context.configure`'s long
# keyword list - which is a signature this dict deliberately only covers part of.
COMPARE_OPTIONS: dict[str, Any] = {
    "compare_type": True,
    "compare_server_default": False,
}


class SchemaOutOfDate(RuntimeError):
    """This database is not at the revision this code was written against."""


def alembic_config(
    *, connection: Connection | None = None, url: str | None = None
) -> Config:
    """A `Config` with no ini file behind it.

    `script_location` is the only main option ever set, and the database URL is
    deliberately never one. `Config` is a `ConfigParser` underneath, so every
    value read back through `get_main_option` is interpolated: a `%` in a
    percent-encoded password would raise, and cocco's URL is very often the
    *empty* `postgresql+psycopg://` that libpq resolves from `PGUSER`,
    `PGDATABASE` and `PGHOST` - a value with no business being written down at
    all. So the URL travels in `attributes`, which is a plain dict, or it is not
    passed and `env.py` reads `cocco.toml` for itself.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    cfg.attributes["url"] = url
    return cfg


def head_revisions() -> set[str]:
    """What the revision scripts in this package say the schema should be."""
    return set(ScriptDirectory.from_config(alembic_config()).get_heads())


def applied_revisions(connection: Connection) -> set[str]:
    """What `alembic_version` says this database is at.

    An empty set for a database that has no such table - `get_current_heads`
    reports rather than creates, which matters: a check that created
    `alembic_version` as a side effect would leave a fresh database claiming to
    be at base when nothing had been run.
    """
    return set(MigrationContext.configure(connection).get_current_heads())


def check_schema_is_current(connection: Connection, *, process: str) -> None:
    """Raise `SchemaOutOfDate` unless this database is exactly at head.

    Takes a **sync** `Connection`, which is what lets both processes share it:
    `cocco-judge` has one already, and `cocco-web` reaches it through
    `AsyncConnection.run_sync`, which forwards the keyword.

    Three different refusals, because they have three different fixes: an empty
    database needs an upgrade to exist at all, a database behind head needs the
    same command, and a database *ahead* of head needs newer code - upgrading
    the schema is not the answer, and saying "run upgrade head" there would be
    advice that cannot work.
    """
    heads = head_revisions()
    applied = applied_revisions(connection)
    if applied == heads:
        return

    config_file = os.environ.get("COCCO_CONFIG", "cocco.toml")
    hint = f"COCCO_CONFIG={config_file} cocco-migrate upgrade head"

    if not applied:
        # Covers both "no `alembic_version` table" and "the table is there and
        # empty", which is where a `downgrade base` leaves things. The two are
        # the same situation to anybody reading this, and they have the same fix.
        raise SchemaOutOfDate(
            f"{process}: this database is at no revision at all - it has no "
            f"schema yet. Build one with:\n    {hint}"
        )

    known = {
        script.revision
        for script in ScriptDirectory.from_config(alembic_config()).walk_revisions()
    }
    if applied - known:
        raise SchemaOutOfDate(
            f"{process}: this database is at "
            f"{', '.join(sorted(applied))}, which is not a revision this cocco "
            f"knows about - it was migrated by a newer one. Upgrade the code; "
            f"do not downgrade the database."
        )

    raise SchemaOutOfDate(
        f"{process}: this database is at {', '.join(sorted(applied))} and the "
        f"code expects {', '.join(sorted(heads))}. Run:\n    {hint}"
    )


def upgrade_to_head(connection: Connection) -> None:
    """`upgrade head` on a connection somebody else owns.

    Which is how `[dev] reset_db` and the drift test run migrations without
    building a second engine to a database they already hold open. `env.py`
    picks the connection up out of `attributes`.
    """
    command.upgrade(alembic_config(connection=connection), "head")


def reset_schema(connection: Connection) -> None:
    """Take the whole schema out: tables, `alembic_version` and the ENUM types.

    Deliberately **not** `metadata.drop_all`. That drops the tables and, with
    them, the PG ENUM types - but `alembic_version` is not in `metadata`
    and survives, so the `upgrade head` that follows finds a database already
    claiming to be at head and does nothing at all. The result is an empty
    database that passes `check_schema_is_current` and 500s on the first query,
    which is worse than anything this module exists to prevent. One statement
    that cannot forget a piece is the fix.

    Development only, and it needs to own the schema - true of the devShell,
    where `PGUSER` is `postgres`. A deployment cannot run it, which is right:
    a deployment has no `[dev]` table.
    """
    connection.execute(text("DROP SCHEMA public CASCADE"))
    connection.execute(text("CREATE SCHEMA public"))


########################################################################
# the command


def cmd_upgrade(args) -> None:
    command.upgrade(alembic_config(), args.revision, sql=args.sql)


def cmd_downgrade(args) -> None:
    command.downgrade(alembic_config(), args.revision, sql=args.sql)


def cmd_current(args) -> None:
    command.current(alembic_config(), verbose=args.verbose)


def cmd_history(args) -> None:
    command.history(alembic_config(), verbose=args.verbose)


def cmd_heads(args) -> None:
    command.heads(alembic_config(), verbose=args.verbose)


def cmd_stamp(args) -> None:
    command.stamp(alembic_config(), args.revision)


def cmd_revision(args) -> None:
    command.revision(
        alembic_config(), message=args.message, autogenerate=args.autogenerate
    )


def cmd_compress_storage(args) -> None:
    """Bring a storage root forward to compressed storage.

    A subcommand here rather than a fifth entry point, and it earns the place:
    this is the command a deployment already runs before starting the servers,
    it already reads `COCCO_CONFIG` for it, and `[storage] root` is in the same
    file `[database] url` is. What it moves is not the schema but the files
    beside it, which is the other half of the same upgrade - and `cocco-web`
    and `cocco-judge` refuse to start until it has been run, exactly as they
    refuse a database that is behind.

    Idempotent, and restartable: a file that already begins with the gzip magic
    is left alone, and each one is written to a sibling and `os.replace`d, so
    an interrupted run leaves every file either wholly old or wholly new. Run
    it again and it finishes the job.
    """
    from cocco.config import config_path, load_config
    from cocco.storage import (
        FORMAT_GZIP,
        FORMAT_MARKER,
        compress_file,
        is_compressed,
    )

    root = load_config(config_path()).storage.root
    if not root.is_dir():
        print(f"cocco-migrate: no storage root at {root}", file=sys.stderr)
        return

    marker = root / FORMAT_MARKER
    done = skipped = before = after = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == marker:
            continue
        if is_compressed(path):
            skipped += 1
            continue
        if args.dry_run:
            done += 1
            before += path.stat().st_size
            continue
        was, now = compress_file(path)
        done += 1
        before += was
        after += now

    if args.dry_run:
        print(
            f"would compress {done} file(s), {before} bytes; "
            f"{skipped} already compressed"
        )
        return

    marker.write_text(FORMAT_GZIP + "\n")
    print(
        f"compressed {done} file(s), {before} -> {after} bytes; "
        f"{skipped} already compressed. {root} is marked as {FORMAT_GZIP}."
    )


def build_parser() -> argparse.ArgumentParser:
    """Our own argparse over `alembic.command.*`, rather than alembic's
    `CommandLine`.

    That class builds its own `Config` from an `--config` ini path, which is
    precisely the thing this design does not have, and its `--help` advertises
    `-c`, `-n` and `-x` - three flags naming a file that does not exist here, so
    anyone who read it would be told to do the wrong thing. Declaring the seven
    subcommands we actually support costs less than suppressing the rest.
    """
    parser = argparse.ArgumentParser(
        prog="cocco-migrate",
        description=(
        "Move the cocco database schema between revisions, and the files "
        "beside it into the format this version reads."
    ),
    )
    # The same flag, with the same help text, as `cocco-web` and `cocco-judge`:
    # the database is the thing all three have to agree about, and the way they
    # agree is by reading one file.
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to the configuration file, which is where the database "
            "comes from (default: $COCCO_CONFIG, else cocco.toml)"
        ),
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity for this run (default: [log] level, else INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    upgrade = sub.add_parser("upgrade", help="apply revisions, normally `head`")
    upgrade.add_argument("revision", nargs="?", default="head")
    upgrade.add_argument(
        "--sql",
        action="store_true",
        help="print the SQL instead of running it, for someone else to apply",
    )
    upgrade.set_defaults(run=cmd_upgrade)

    downgrade = sub.add_parser("downgrade", help="revert to an earlier revision")
    # No default, unlike `upgrade`: this one has to be typed out.
    downgrade.add_argument("revision")
    downgrade.add_argument("--sql", action="store_true", help="print the SQL instead")
    downgrade.set_defaults(run=cmd_downgrade)

    for name, run, help_text in (
        ("current", cmd_current, "the revision this database is at"),
        ("history", cmd_history, "every revision, newest first"),
        ("heads", cmd_heads, "the revision this code expects"),
    ):
        listing = sub.add_parser(name, help=help_text)
        listing.add_argument("-v", "--verbose", action="store_true")
        listing.set_defaults(run=run)

    revision = sub.add_parser("revision", help="write a new revision script")
    revision.add_argument("-m", "--message", required=True)
    revision.add_argument(
        "--autogenerate",
        action="store_true",
        help=(
            "diff cocco/model.py against the database this config names. A "
            "development command: it writes into cocco/migrations/versions/, "
            "which outside an editable install is inside site-packages."
        ),
    )
    revision.set_defaults(run=cmd_revision)

    stamp = sub.add_parser(
        "stamp",
        help=(
            "record a revision without running it, for a database whose schema "
            "already matches it"
        ),
    )
    stamp.add_argument("revision", nargs="?", default="head")
    stamp.set_defaults(run=cmd_stamp)

    compress = sub.add_parser(
        "compress-storage",
        help=(
            "compress the test case files under [storage] root, which is what "
            "this version of cocco reads"
        ),
    )
    compress.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be compressed and write nothing",
    )
    compress.set_defaults(run=cmd_compress_storage)

    return parser


def main(argv: list[str] | None = None) -> int:
    from cocco.config import ConfigError, config_path, load_log_config
    from cocco.logs import configure_logging

    args = build_parser().parse_args(argv)

    # Before any alembic command, because `env.py` reads the file for itself.
    # `cocco/server.py`'s trick, for `cocco/server.py`'s reason: the setting has
    # to be in place before the thing that reads it is reached.
    if args.config is not None:
        os.environ["COCCO_CONFIG"] = str(args.config)

    # There is no ini file, so alembic's usual `fileConfig()` never runs and
    # nothing else would configure logging at all - without this, `upgrade` is
    # silent about the revisions it just applied. The same `[log]` table as the
    # two servers, but onto stderr: `--sql` prints DDL on stdout, to be
    # redirected into a file that must not have log lines in it.
    try:
        configure_logging(
            load_log_config(config_path()),
            "migrate",
            args.log_level,
            stream="ext://sys.stderr",
        )
    except ConfigError as e:
        print(f"cocco-migrate: {e}", file=sys.stderr)
        return 1

    try:
        args.run(args)
    except CommandError as e:
        print(f"cocco-migrate: {e}", file=sys.stderr)
        return 1
    except SQLAlchemyError as e:
        # A refusal from the database itself - most often a revision meeting a
        # table that is already there, which is what a schema built by
        # `metadata.create_all` and never stamped looks like. A traceback
        # through alembic, sqlalchemy and psycopg buries the one line that says
        # so, and none of those frames is ours.
        print(f"cocco-migrate: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
