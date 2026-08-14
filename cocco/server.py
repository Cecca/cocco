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
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn


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
    parser.add_argument(
        "--proxy-headers",
        action="store_true",
        help=(
            "Trust X-Forwarded-* from the reverse proxy. Without it a "
            "TLS-terminating proxy makes the request say `http`, and the "
            "OAuth redirect URI cocco builds will not match the one Google "
            "has registered - see [site] base_url in cocco.example.toml."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging verbosity (default: INFO)",
    )
    return parser


def _check_schema() -> None:
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

    from cocco.config import config_path, load_config
    from cocco.migrate import SchemaOutOfDate, check_schema_is_current

    config = load_config(config_path())
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
        print(e, file=sys.stderr)
        raise SystemExit(1)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    # Before the import string is resolved, which is the point of this module.
    # Set rather than defaulted: with no flag, whatever COCCO_CONFIG already
    # says stands, and `config_path()` falls back to `cocco.toml` as always.
    if args.config is not None:
        os.environ["COCCO_CONFIG"] = str(args.config)

    _check_schema()

    uvicorn.run(
        "cocco.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers,
        proxy_headers=args.proxy_headers,
        log_level=args.log_level.lower(),
    )


if __name__ == "__main__":
    main()
