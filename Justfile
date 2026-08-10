set shell := ["bash", "-euo", "pipefail", "-c"]

set dotenv-load

log_dir := ".logs"

# Storage root shared by the web app and the judge; they must agree on it.
export COCCO_STORAGE := env_var_or_default("COCCO_STORAGE", "/tmp/cocco-storage")

# Development runs with authentication bypassed, so `just dev` needs no Google
# credentials. COCCO_DUMMY_AUTH=0 exercises the real flow; COCCO_DUMMY_USER
# picks who you are signed in as.
export COCCO_DUMMY_AUTH := env_var_or_default("COCCO_DUMMY_AUTH", "1")
export COCCO_DUMMY_USER := env_var_or_default("COCCO_DUMMY_USER", "alice@unipd.it")

# Start PostgreSQL, the web app and the judge; logs land in .logs/
default: dev

# Run the whole development stack until Ctrl-C
dev:
    #!/usr/bin/env bash
    set -euo pipefail

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

    echo "app:      http://127.0.0.1:8000 -> {{log_dir}}/app.log"
    uv run fastapi dev cocco/main.py >{{log_dir}}/app.log 2>&1 &
    pids+=($!)

    echo "judge:    running             -> {{log_dir}}/judge.log"
    uv run python -m cocco.judge \
        --database "postgresql+psycopg://$PGUSER@/$PGDATABASE?host=$PGHOST" \
        --storage "$COCCO_STORAGE" >{{log_dir}}/judge.log 2>&1 &
    pids+=($!)

    echo
    echo "Ctrl-C to stop everything. Follow the logs with: just logs"

    # Bring the whole stack down as soon as any one process dies.
    wait -n || true
    echo "a process exited, shutting the stack down" >&2
    exit 1

# Follow the logs of every process
logs:
    tail -n 20 -F {{log_dir}}/postgres.log {{log_dir}}/app.log {{log_dir}}/judge.log
