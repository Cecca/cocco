set shell := ["bash", "-euo", "pipefail", "-c"]

set dotenv-load

log_dir := ".logs"

# Which configuration file both processes read: the database, the storage root
# and the development shortcuts all come out of it, so this one variable is
# what makes the web app and the judge agree. `cocco.dev.toml` is in git and
# bypasses authentication, so `just web` needs no Google credentials.
#
# To run against your own settings - the real sign-in flow, another database -
# write a `cocco.toml`, which is gitignored, and start with
#   COCCO_CONFIG=cocco.toml just web
export COCCO_CONFIG := env_var_or_default("COCCO_CONFIG", "cocco.dev.toml")

# Start the development database
start-dev-db:
    docker compose up -d

# Stop the development database
stop-dev-db:
    docker compose down

# Start the web worker
web:
    uv run cocco-web

# Start the judge worker
judge:
    uv run cocco-judge

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
