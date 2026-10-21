# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`cocco` is a self-hosted judge for programming challenges. Submissions run inside the [isolate](https://github.com/ioi/isolate) sandbox (the IOI/ICPC judge), with language runtimes from a Nix flake. HTTP is FastAPI + Jinja + htmx; persistence is SQLAlchemy 2.0 Core on PostgreSQL.

`DESIGN.md` has the rationale for isolate + Nix and the rejected alternatives; read it before proposing architectural changes.

## Commands

Dependencies are managed with `uv` (Python 3.13). A `.envrc` / Nix `devShell` auto-creates and activates `.venv`.

- Install deps: `uv sync`
- Start / stop PostgreSQL: `pg-start`, `pg-stop` (then `create-db cocco-dev` once), or the `compose.yaml` container with `just start-dev-db` / `just stop-dev-db`
- Run the stack: `just migrate`, then `just web` and `just judge` in two terminals. The Justfile exports `COCCO_CONFIG=cocco.dev.toml` unless already set.
- Run the API alone: `COCCO_CONFIG=cocco.dev.toml uv run cocco-web --reload`
- Run the judge alone: `COCCO_CONFIG=cocco.dev.toml uv run cocco-judge`
- Apply a schema change: `just migration "what changed"` then `just migrate` (see `.claude/rules/migrations.md`)
- Ask where a database stands: `uv run cocco-migrate current` (or `heads`, `history`, `downgrade`)
- Reset and reseed the database: set `[dev] reset_db = true`, start once, set it back
- Configure a deployment: `cp cocco.example.toml cocco.toml` and edit it; secrets go in the environment, not the file
- Run all tests: `uv run pytest`
- Run everything but the judge: `uv run pytest -m "not integration"` (still needs PostgreSQL; skips cleanly without one)
- Run a single test: `uv run pytest cocco/test_judge.py::test_language_python`
- Build a language runtime on its own: `nix build .#python3` (or `.#gcc`, `.#jdk`)
- Build the CSS: `npm install` once, then `npm run build:css` (or `npm run watch:css`)
- Build the wheel and sdist: `just build`, which is `npm run build:css` *then* `uv build` — the order matters
- Drive a challenge from the command line: `COCCO_URL=http://localhost:8000 uv run cocco ls` (then `pull` / `push`)

**cocco is a package with four commands**: `cocco` (`cli.py`), `cocco-web` (`server.py`), `cocco-judge` (`judge.py:main`), `cocco-migrate` (`migrate.py`). Everything read at runtime lives **inside `cocco/`**, resolved from `Path(__file__).resolve().parent`, never the working directory. There is deliberately **no `alembic.ini`**.

**Tests run on PostgreSQL only** (don't add an SQLite path), in their own `cocco-test` database that is truncated between tests. `@pytest.mark.integration` means isolate and nix.

The web app and the judge are **separate processes** on the same config file. Submitting only enqueues a row; nothing leaves `queued` without a judge on the same database.

**Config: the file is settings, the environment is secrets.** Secrets (`COCCO_SECRET_KEY`, OAuth client ids/secrets) have no file spelling. Keep `[dev] reset_db` false.

## Guides

Each area has a guide in `.claude/rules/`, loaded when you touch its files. Read the guide before changing that area:

- `packaging-and-config.md` — entry points, wheel contents, `cocco-web` vs `cocco.main`, config file and secrets
- `testing.md` — the `cocco-test` database, truncation, the `integration` marker
- `data-model.md` — blacklist columns, timestamps, Pydantic mirrors
- `pages.md` — each page and its routes: challenge, submission, home, enrolling, admin, editors, danger zones, people, profile
- `authorization.md` — enrollment gate, refusals, administrators, blocks, `next`, CSRF
- `domain-rules.md` — ordering and numbering, retiring test cases, regrading, deletion cascade, archived, visible, blocks, scores, leaderboard, hidden outputs
- `storage.md` — gzip under the storage root
- `frontend.md` — CSS build and static URLs
- `judge.md` — execution pipeline, evaluators, isolate boxes
- `migrations.md` — Alembic revisions and what autogenerate misses
- `auth.md` — sign-in: OAuth providers, passwords, dummy auth, tokens
- `transfer.md` — the `/api` zip format and the `cocco` CLI
- `exercises.md` — live Python exercises
- `logging.md` — JSON logs, access log, proxy headers

## Architecture

### Data model (`cocco/model.py`)

Core `Table`s on one `MetaData`: `User` (roles via `Enrollment`; `Administrator` beside it, keyed on email so it can name someone who never signed in), `Course` → `Challenge` → `TestCase` (files referenced by path, not stored in the DB; `is_sample` = downloadable by students, `is_active` = graded), `Submission` → `TestOutcome`. No queue table: queued means `status == SubmissionStatus.queued`. `Challenge.ranking` (`LeaderboardRanking`) is presentation-only; the judge never reads it. The `exercise_*` tables share nothing with this above the execution layer.

**Every timestamp is `timestamptz` written by Postgres** (`server_default=func.now()`), never `datetime.now()`.

### Queries (`cocco/repository/`)

Every query lives in this package, one module per area: `rules` (shared Core fragments such as `_effective_archived_at`, `_challenge_visible`), `judging` (the judge's `repo`: sync helpers, plus the exercise jobs), `users`, `credentials` (passwords, tokens), `courses`, `challenges`, `exercise_sets`, `exercise_sessions`, and `lifecycle` (archiving and every hand-written deletion cascade). `__init__.py` re-exports nothing: import from the module that owns the area.

Page-level queries Page-level queries build one nested JSON document with `json_agg` / `row_to_json`, returning a dict or list that templates walk with attribute syntax (`DESIGN.md` "Database design"). New page queries follow that shape rather than making several round trips. They are **Postgres-specific**. A shared rule or CTE is a small function returning a Core object (`_course_challenges`, `_session_checks`, …), its dependencies passed as arguments, never a string spliced into `text()`. Page queries that share nothing may still be `text()`; the leaderboard's `_LEADERBOARD_CRITERIA` is enum-keyed literal SQL by design. Judge-side helpers (`pull_queued_submission`, `insert_test_outcomes`, `mark_submission_done`) are plain Core on a *sync* `Connection`; web-side ones are `async` on an `AsyncConnection`.

Insert helpers (`insert_enrollment`, `insert_submission`) don't commit; the caller owns the transaction. `get_db` yields a bare connection with no transaction, so a writing route must `await db.commit()`.

### HTTP layer (`cocco/main.py`, `cocco/web/`)

FastAPI app `cocco.main:app`. `main.py` is the composition root: it builds the app, adds the middlewares, mounts `/static`, registers the exception handlers and the startup hook, and calls `include_router` once per module of `cocco/web/`. The routes live there as `APIRouter` modules: `core` (config, engine, `Storage`, `get_db`, shared constants) and `templating` at the bottom; `access` (identity, refusals, gates) above every area; then `signin`, `home`, `admin`, `courses`, `people`, `exercise_sets`, `exercise_sessions`, `exercise_student`, `challenges`, `challenge_editor`, `testcases`, `profile`, `api`, plus `seed` (schema check and dev data). Imports run one way (e.g. `courses` → `home`, `testcases`); nothing under `web/` imports `cocco.main`. Loggers are `cocco.web.<module>`.

**Seams.** `CONFIG`, `DUMMY_AUTH` and `storage` are read through `core` (`core.CONFIG`), `oauth_client` through `signin`; a test patches the module whose code looks the name up, and `main.py` re-exports nothing, so patching `main.X` reaches nothing.

Jinja templates in `cocco/templates/`, Tailwind v4 + daisyUI v5 (`assets/styles.css` → `cocco/static/styles.css`), htmx.

Pages return full documents. Anything live is an `f-*.jinja` fragment served from `/f/...` and polled with `hx-get` + `hx-trigger="load, every Ns"` (that's how a submission moves `queued` → `running` → score). An include-only partial gets a `_` prefix (e.g. `_statement.jinja`, shared by both challenge views).

**htmx rules used throughout:**
- Errors a user should see are **200 with an alert**, not 4xx: htmx doesn't swap error responses. This stops at htmx: `/api` routes refuse with real status codes and JSON, so a CLI can't mistake a refusal for success.
- A form field that may be empty is a **defaulted** `Form` field, not required: a missing field is a 422, whose body htmx drops (e.g. `post_administrator`'s `email`).
- A filter input sits **outside** the element it swaps (a rebuilt input loses focus), and so does any form beside a filtered list (the filter would wipe a half-typed value). Every mutation on a filtered list carries `q`.
- A page with mutations has **one swap target**, and every route on it answers with the same fragment, alert included, so message and new state arrive in one swap: `/admin` → `#users` / `f-users.jinja`; people page → `#people` / `f-course-people.jinja`; profile → `#identity`.
- Navigation after a mutation is `204` + `HX-Redirect`.

**Route order is load-bearing.** `/courses/{course_id}` would swallow `/courses/new`, and `/courses/{course_id}/{challenge_id}` matches any single segment. So `/courses/new` is declared above `get_course`, and every other two-segment course page (`/edit`, `/people`, the course danger-zone routes, exercises and sessions) above `get_challenge`. Since the routes are spread over modules, the order is `include_router` order in `main.py` (`courses`, `people`, the exercise modules, then `challenges`); `test_no_route_is_shadowed_by_an_earlier_one` in `test_main.py` checks it. `/admin` collides with nothing.

### Authorization

`role` lives on `enrollment`, so permissions are **per course**. `EDITOR_ROLES` (teacher, collaborator) is course staff; students read and submit. The only site-wide power is the **administrator**, deliberately not a `UserRole`. Administrators count as `teacher` on every course, only via `effective_course_role` / `effective_challenge_role`. Non-members are shut out of a course's contents, not read-only. Every route depends on `CurrentUser` or `ApiUser`.

**Hiding a control is presentation, never access control**: anything mutating a challenge goes through `require_challenge_editor`, which also guards the staff view's *reads* (standings, per-student pages). The exception is the submission page: `get_submission` and its fragment use `authorize_submission` (author, else `EDITOR_ROLES`). It needs no membership check, since `post_submission` was gated.

## Conventions specific to this repo

- **Box ids are a shared resource.** Always go through `_IDS_POOL.acquire()`, never a hardcoded id; the pool defaults to 32.
- **Language runtimes are pinned by `flake.lock`.** Change a compiler by updating the flake, not the system.
- **Large inputs live on disk.** Test files are passed by path and reach a box in chunks. Uploads go through `Storage.save_stream`, never `await upload.read()`; files leaving the app (sample download, export zip) are streamed. **Exercises are the deliberate exception — don't "fix" it**: their inputs are a few typed bytes stored in the version's compiled document, read in one query (`query_job_exercise`), with no `Storage` involved.
- **All file access goes through `Storage`.** `Storage.resolve` refuses paths outside the root, and `safe_filename` reduces a user-supplied name to a bare one. Test case paths come from teacher input; never join them onto a directory by hand. Stored files are gzip with no `.gz` suffix; only `cocco/storage.py` knows.
- **No FK declares `ON DELETE`**, deliberately. Deletion cascades are hand-written (see `domain-rules.md`).
- **The judge never learns about archived, hidden or blocked.** `pull_queued_submission` doesn't filter on any of them: skipping rows would strand them as `queued` forever, polluting standings and the regrade guard.
- **A secret has one source and never a fixed default.** No secret goes in a file or the code.
- **CSS is a build artifact.** Edit `assets/styles.css`, never `cocco/static/styles.css`, and never assemble class names at runtime (Tailwind only emits classes it sees in the source).
