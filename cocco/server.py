"""The `cocco-web` console script: uvicorn, pointed at `cocco.main:app`.

Its own module, and that is the whole design. `cocco.main` reads its
configuration **at import** - `CONFIG`, the async engine, `Storage(...)`
(which creates the storage root) and `load_languages()` are all module-level -
so a `--config` flag parsed from inside it would arrive after the file it
names had already been read. Here the argument comes first and `COCCO_CONFIG`
is in place before `cocco.main` is imported at all.

Which is also why uvicorn is handed the *import string* `"cocco.main:app"`
rather than the object: it is the only form `--reload` and `--workers` accept,
since both of those construct the app in another process, and it keeps the
import lazy, which is what makes `--config` mean anything.

The counterpart of `cocco.judge:main`, and deliberately spelled like it: the
same `--config`, with the same help text, because the two processes reading
one file is the thing that keeps them agreeing.

Logging reaches uvicorn the same way the app does: as data, not as state. The
`dictConfig` dict from `cocco.logs` goes in as `log_config=`, which uvicorn
applies in every process it starts - a handler set up here would stay here,
and the app under `--reload` or `--workers` runs somewhere else.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn

logger = logging.getLogger("cocco.server")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cocco-web",
        description="Serve the cocco web application.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to the configuration file, which is where the database "
            "and the storage root come from (default: $COCCO_CONFIG, else "
            "cocco.toml)"
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Address to bind (default: 127.0.0.1). Use 0.0.0.0 to accept "
            "connections from outside this machine."
        ),
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to bind (default: 8000)"
    )
    # uvicorn cannot do both: `--reload` runs one worker under a file watcher.
    # Refusing here beats uvicorn accepting the pair and quietly ignoring one.
    how = parser.add_mutually_exclusive_group()
    how.add_argument(
        "--reload",
        action="store_true",
        help="Restart on source changes. Development only.",
    )
    how.add_argument(
        "--workers",
        type=int,
        default=None,
        help=(
            "Number of worker processes. They must share a session signing "
            "key, so set COCCO_SECRET_KEY or every worker will disagree "
            "about who is signed in."
        ),
    )
    # Deprecated: `[site] proxy_headers` in the file is the setting now, and
    # this flag only survives one release so a unit file that still passes it
    # keeps working - see `_warn_about_proxy_headers_flag`.
    parser.add_argument(
        "--proxy-headers",
        action="store_true",
        help="Deprecated: set `[site] proxy_headers = true` in the file instead.",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity for this run (default: [log] level, else INFO)",
    )
    return parser


def _check_storage(config) -> None:
    """Refuse to serve a storage root this version cannot read.

    `_check_schema`'s twin, here for exactly its reason: the app checks again
    at startup, and under `--reload` or `--workers` that check cannot become a
    non-zero exit. The two are separate functions because they answer about
    different halves of a deployment - the database and the files beside it -
    and either can be behind without the other.
    """
    from cocco.storage import StorageFormatError, check_storage_format

    try:
        check_storage_format(config.storage.root)
    except StorageFormatError as e:
        logger.error("%s", e)
        raise SystemExit(1)


def _check_schema(config) -> None:
    """Refuse to serve a database that is not at the revision this code expects.

    This looks like a duplicate of the check in `cocco.main`'s startup, and it
    is not: uvicorn only turns a failed startup into a non-zero exit when there
    is one worker and no reloader. Under `--reload` - which is what `just dev`
    runs - the reload supervisor outlives the dead child, does not restart it
    (it restarts on file changes, not on a child dying) and eventually exits 0,
    so `just dev`'s `wait -n` never fires and what you get is a silently absent
    web app. Under `--workers N` the process manager respawns the failing worker
    forever. Asking here, before uvicorn exists, is what makes the refusal a
    plain non-zero exit under every combination of flags; the copy in the app is
    what covers every other ASGI runner and makes each worker verify for itself.

    Imported inside the function for this module's usual reason - importing
    `cocco.server` must stay cheap, and `--config` has to be in the environment
    first. Pointedly not `cocco.main`: this needs the configuration file and the
    revision scripts, not the app, which is what
    `test_the_server_does_not_import_the_app` is about.
    """
    from sqlalchemy import create_engine

    from cocco.migrate import SchemaOutOfDate, check_schema_is_current

    if config.dev.reset_db:
        # A reset builds the schema itself, so it is allowed to start behind -
        # indeed it is normally started against nothing at all.
        return

    engine = create_engine(config.database.url)
    try:
        with engine.connect() as conn:
            check_schema_is_current(conn, process="cocco-web")
    except SchemaOutOfDate as e:
        # No prefix of our own: `check_schema_is_current` was given the process
        # name and has already put it at the front of the sentence.
        logger.error("%s", e)
        raise SystemExit(1)
    finally:
        engine.dispose()


def _warn_about_proxy_headers_flag() -> None:
    """`config.py`'s `_warn_about_removed_settings`, for the one setting that
    was a flag rather than a variable: named out loud, and only warned about,
    because a unit file that still passes it must not stop the app booting.

    The flag still does what it did - uvicorn trusts `[site] trusted_proxies`
    for the client address and the scheme - but only the file's spelling also
    lets the app adopt nginx's X-Request-ID, since that needs to see the peer
    before uvicorn has rewritten it.
    """
    logger.warning(
        "--proxy-headers is deprecated; that setting is `[site] proxy_headers "
        "= true` in the configuration file now"
    )


def main(argv: list[str] | None = None) -> None:
    from cocco.config import ConfigError, config_path, load_config, load_log_config
    from cocco.logs import configure_logging, logging_config

    args = build_parser().parse_args(argv)

    # Before the import string is resolved, which is the point of this module.
    # Set rather than defaulted: with no flag, whatever COCCO_CONFIG already
    # says stands, and `config_path()` falls back to `cocco.toml` as always.
    if args.config is not None:
        os.environ["COCCO_CONFIG"] = str(args.config)

    # Logging first, from the `[log]` table alone, so that everything below -
    # `load_config`'s warnings, the two refusals - comes out in the format the
    # deployment asked for. A `[log]` table that cannot be read is the one
    # refusal that has to be a bare `print`: there is no log to write it to.
    try:
        log = load_log_config(config_path())
        configure_logging(log, "web", args.log_level)
        config = load_config(config_path())
    except ConfigError as e:
        print(f"cocco-web: {e}", file=sys.stderr)
        raise SystemExit(1)

    if args.proxy_headers:
        _warn_about_proxy_headers_flag()

    _check_schema(config)
    _check_storage(config)

    uvicorn.run(
        "cocco.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        # The same dict as above, applied again by uvicorn in every process it
        # starts - and over `uvicorn.error`, so there is one format in the
        # stream. `log_level` stays None: it would override the dict's levels.
        log_config=logging_config(config.log, "web", args.log_level),
        log_level=None,
        # `cocco.access`, from the app's own middleware, replaces this line.
        access_log=False,
        # Normally False: `[site] proxy_headers` is handled inside the app, by
        # `cocco.main`'s middleware, which is what lets it see the proxy's own
        # address before the client's replaces it. Only the deprecated flag
        # still asks uvicorn to do it - trusting the file's list, never
        # FORWARDED_ALLOW_IPS from the environment.
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=list(config.trusted_proxies),
    )


if __name__ == "__main__":
    main()
