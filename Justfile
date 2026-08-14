set shell := ["bash", "-euo", "pipefail", "-c"]

set dotenv-load

log_dir := ".logs"

# Which configuration file both processes read: the database, the storage root
# and the development shortcuts all come out of it, so this one variable is
# what makes the web app and the judge agree. `cocco.dev.toml` is in git and
# bypasses authentication, so `just dev` needs no Google credentials.
#
# To run against your own settings - the real sign-in flow, another database -
# write a `cocco.toml`, which is gitignored, and start with
#   COCCO_CONFIG=cocco.toml just dev
export COCCO_CONFIG := env_var_or_default("COCCO_CONFIG", "cocco.dev.toml")

# Start PostgreSQL, the web app and the judge; logs land in .logs/
default: dev

# Run the whole development stack until Ctrl-C
dev:
    #!/usr/bin/env bash
    set -euo pipefail

    # These start the cluster - and, since cocco.dev.toml's database URL is
    # empty, they are also what libpq resolves it from.
    : "${PGDATA:?PGDATA not set — enter the dev shell first}"
    : "${PGHOST:?PGHOST not set — enter the dev shell first}"
    : "${PGUSER:?PGUSER not set — enter the dev shell first}"
    : "${PGDATABASE:?PGDATABASE not set — enter the dev shell first}"

    mkdir -p {{log_dir}}

    pg_started=0
    pids=()

    # The children stay in this recipe's process group, so a Ctrl-C in the
    # terminal already reaches all of them; this is the belt and braces for
    # every other way out.
    cleanup() {
        trap - EXIT INT TERM
        for pid in "${pids[@]:-}"; do
            kill -TERM "$pid" 2>/dev/null || true
        done
        if [ "$pg_started" -eq 1 ]; then
            pg-stop >>{{log_dir}}/postgres.log 2>&1 || true
        fi
        wait 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 0' INT TERM

    if pg_isready -q -h "$PGHOST" -U "$PGUSER" -d postgres; then
        echo "postgres: already running, leaving it alone"
    else
        echo "postgres: starting            -> {{log_dir}}/postgres.log"
        pg-start >{{log_dir}}/postgres.log 2>&1 &
        pids+=($!)
        pg_started=1
        for _ in $(seq 1 60); do
            pg_isready -q -h "$PGHOST" -U "$PGUSER" -d postgres && break
            sleep 0.5
        done
        if ! pg_isready -q -h "$PGHOST" -U "$PGUSER" -d postgres; then
            echo "postgres: did not come up, see {{log_dir}}/postgres.log" >&2
            exit 1
        fi
    fi

    create-db "$PGDATABASE" >>{{log_dir}}/postgres.log 2>&1

    # Before either process, and not beside them: both refuse to start against a
    # database that is not at head, so this is what makes a fresh checkout come
    # up at all. `set -e` means a failed migration takes the whole stack down
    # through the cleanup trap rather than starting an app that cannot work.
    echo "schema:   upgrading           -> {{log_dir}}/migrate.log"
    uv run cocco-migrate upgrade head >{{log_dir}}/migrate.log 2>&1

    echo "app:      http://127.0.0.1:8000 -> {{log_dir}}/app.log"
    uv run cocco-web --reload >{{log_dir}}/app.log 2>&1 &
    pids+=($!)

    # No --config on either: both read $COCCO_CONFIG, the same file, so there
    # is no way to hand the two of them different databases. And both are the
    # installed console scripts rather than `fastapi dev` and `python -m`, so
    # what a day's development exercises is what a deployment runs.
    echo "judge:    running             -> {{log_dir}}/judge.log"
    uv run cocco-judge >{{log_dir}}/judge.log 2>&1 &
    pids+=($!)

    echo
    echo "Ctrl-C to stop everything. Follow the logs with: just logs"

    # Bring the whole stack down as soon as any one process dies.
    wait -n || true
    echo "a process exited, shutting the stack down" >&2
    exit 1

# Follow the logs of every process
logs:
    tail -n 20 -F {{log_dir}}/postgres.log {{log_dir}}/migrate.log {{log_dir}}/app.log {{log_dir}}/judge.log

# Bring the database up to the schema this checkout expects
migrate:
    uv run cocco-migrate upgrade head

# Write a new revision by diffing cocco/model.py against the dev database
#
# Two recipes rather than one `migrate *args`, because just's own parsing makes
# leading `--` flags awkward to pass through. Anything else - `current`,
# `history`, `downgrade` - is `uv run cocco-migrate ...` directly, which works
# because COCCO_CONFIG is exported at the top of this file.
migration message:
    uv run cocco-migrate revision --autogenerate -m "{{message}}"

# Build the stylesheet from assets/styles.css into the package
build-css:
    npm run build:css

# Build the wheel and the sdist into dist/
#
# The stylesheet first, and not as a convenience: `cocco/static/styles.css` is
# gitignored, so it exists only once tailwind has written it, and a wheel built
# without it is one that installs and serves an unstyled site.
build: build-css
    uv build
