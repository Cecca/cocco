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


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    # Before the import string is resolved, which is the point of this module.
    # Set rather than defaulted: with no flag, whatever COCCO_CONFIG already
    # says stands, and `config_path()` falls back to `cocco.toml` as always.
    if args.config is not None:
        os.environ["COCCO_CONFIG"] = str(args.config)

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
