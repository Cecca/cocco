# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`cocco` is a self-hosted judge for programming challenges. Submissions are executed inside the [isolate](https://github.com/ioi/isolate) sandbox (the IOI/ICPC judge), using language runtimes supplied by a Nix flake. The HTTP layer is FastAPI with Jinja templates and htmx; persistence is the SQLAlchemy 2.0 Core API on PostgreSQL.

`DESIGN.md` has the rationale for the isolate + Nix choice and the alternatives that were rejected; read it before proposing architectural changes.

## Commands

Dependencies are managed with `uv` (Python 3.13). A `.envrc` / Nix `devShell` auto-creates `.venv` and activates it.

- Install deps: `uv sync`
- Start / stop PostgreSQL: `pg-start`, `pg-stop` (then `create-db cocco-dev` once), or the `compose.yaml` container with `just start-dev-db` / `just stop-dev-db`
- Run the stack: `just migrate`, then `just web` and `just judge` in two terminals. The Justfile exports `COCCO_CONFIG=cocco.dev.toml` unless it is already set.
- Run the API alone: `COCCO_CONFIG=cocco.dev.toml uv run cocco-web --reload`
- Run the judge alone: `COCCO_CONFIG=cocco.dev.toml uv run cocco-judge`
- Apply a schema change: `just migration "what changed"` then `just migrate` — see "Migrations"
- Ask where a database stands: `uv run cocco-migrate current` (or `heads`, `history`, `downgrade`)
- Reset and reseed the database: set `[dev] reset_db = true`, start once, set it back
- Configure a deployment: `cp cocco.example.toml cocco.toml` and edit it — the three secrets go in the environment, not in it
- Run all tests: `uv run pytest`
- Run everything but the judge: `uv run pytest -m "not integration"` — still needs PostgreSQL, and skips cleanly without one
- Run a single test: `uv run pytest cocco/test_judge.py::test_language_python`
- Build a language runtime on its own: `nix build .#python3` (or `.#gcc`, `.#jdk`)
- Build the CSS: `npm install` once, then `npm run build:css` (or `npm run watch:css`)
- Build the wheel and the sdist: `just build` — which is `npm run build:css` *then* `uv build`, in that order for a reason (see below)
- Drive a challenge from the command line: `COCCO_URL=http://localhost:8000 uv run cocco ls` (then `pull` / `push` — see "The transfer API")

**cocco is a package, and it is four commands.** `[project.scripts]` declares `cocco` (`cocco/cli.py`, the transfer CLI), `cocco-web` (`cocco/server.py`), `cocco-judge` (`cocco/judge.py:main`) and `cocco-migrate` (`cocco/migrate.py`); hatchling builds it and `uv sync` installs it editable into `.venv`, which is what makes those names work in the checkout as well as out of it. Three processes and the command that prepares the database two of them share — and which those two refuse to start without. That is not just four lines of metadata: everything the server processes read at runtime — `templates/`, `static/`, `languages.toml`, `cocco/evaluators/`, `cocco/migrations/` — lives **inside `cocco/`** and is resolved with `Path(__file__).resolve().parent`, never against the working directory, because an installed wheel has no repository root to be relative to. The migrations are the newest member of that list and the one with a tempting alternative: there is deliberately **no `alembic.ini`**, at the repository root or anywhere else, because an ini file is a working-directory dependency by construction and a stray one would be found by anybody running plain `alembic` from the checkout — who would then migrate whatever database *it* named rather than the one `cocco.toml` does. `assets/` is the other half of that line: it is tailwind's *input* plus the design-source icons, is never read by Python, and is not shipped. The one thing that survives as a genuine build step is `cocco/static/styles.css`, which is gitignored and so invisible to hatchling's file selection — `[tool.hatch.build.targets.*] artifacts` names it explicitly, and it has to have been built before `uv build` runs or the wheel installs a site with no stylesheet.

`cocco-web` is a **thin uvicorn wrapper and its own module**, not a function in `main.py`. `cocco.main` reads its configuration at import — `CONFIG`, the engine, `Storage(...)`, `load_languages()` are all module-level — so a `--config` flag parsed from inside it would arrive after the file it names had been read. `cocco/server.py` puts the path in `COCCO_CONFIG` first and only then hands uvicorn the import string `"cocco.main:app"`, which is also the only form `--reload` and `--workers` accept. `cocco-judge`'s `main` is module-level and sync for the same kind of reason: it used to be an `async def` inside `if __name__ == "__main__"`, which `python -m` can run and an entry point cannot name at all.

**The tests run on PostgreSQL and nothing else**, because the app does. There is no SQLite route any more: twelve of `repository.py`'s page queries are raw Postgres SQL and cannot execute anywhere else, foreign keys and `FOR UPDATE` are enforced by one of the two and silently ignored by the other, and `timestamptz` is a type only one of them has — a test that passed on the other database was not evidence about this one. That is also why every route-test file used to be split in two halves with a paragraph explaining the seam, and now is not.

The tests own **`cocco-test`**, which `cocco/conftest.py` creates on demand and nobody has to make by hand. Host, port and user come out of the environment, so the devShell's socket and `compose.yaml`'s container both work with no configuration; `PGDATABASE` is deliberately **not** read, and neither is `load_config`. That is the whole safety story: the schema is emptied between every test, so the database has to be one that is nobody's but the suite's — not a developer's `cocco-dev`, and certainly not whatever a deployment's `cocco.toml` names. The schema itself is built once per session with `upgrade head`, and each test starts from a `TRUNCATE ... RESTART IDENTITY CASCADE`, which is why the first row of a table is reliably id 1. `test_migrations.py` gets a second database, `cocco-test-migrations`, because its drift test drops the schema on the way out and would otherwise strand whatever ran next.

`@pytest.mark.integration` therefore means **isolate and nix**, and only those: `isolate` installed with its setuid bit, a working `nix` with flakes, and the current user allowed to run isolate boxes. On NixOS, isolate is declared via `security.wrappers`; on other distros install it outside Nix. Everything else is an ordinary test that skips, with a message naming `pg-start`, when no server answers.

The web app and the judge are **separate processes** that must agree on three things: the database, the storage root for test case files (`[storage] root`), and the *installed package* itself — `languages.toml` decides what an uploaded extension means at both ends, and the bundled evaluators in `cocco/evaluators/` are code, so a judge running an older version silently grades with an older `equality.py` than the editor advertises. The first two they agree on by *reading the same file*: the judge takes no `--database` and no `--storage`, only a `--config`, because a flag that can disagree with the file is the same bug as an environment variable that can. The third they agree on by *being the same package*, which is exactly why those files moved inside `cocco/` — a wheel is one version of all of it. Submitting through the web UI only enqueues a row — nothing leaves `queued` unless a judge is running against the same database.

`[dev] reset_db` drops the schema and reseeds dummy data on startup. Leave it false in normal development — `cocco.dev.toml` ships it that way — because `cocco-web --reload` reloads on every file change and each reload would wipe the data. It is not how a schema change reaches a database: that is a migration (see "Migrations"), and `reset_db` is for throwing the *data* away.

**Configuration is split in two, and the line is one sentence: the file is settings, the environment is secrets.** `cocco/config.py` is where both halves are read — its own module rather than a corner of `auth.py`, because the judge reads it too and a database URL is none of sign-in's business. The file carries `[database] url`, `[storage] root`, `[judge] flake`, `[session]`, `[auth]`, `[site]` and `[dev]`; the secrets — `COCCO_SECRET_KEY`, `COCCO_GOOGLE_CLIENT_ID`, `COCCO_GOOGLE_CLIENT_SECRET`, `COCCO_GITHUB_CLIENT_ID`, `COCCO_GITHUB_CLIENT_SECRET` — have **no file spelling at all**, so there is no precedence to reason about and nothing secret in a file that gets copied, diffed and pasted into a bug report. `COCCO_CONFIG` is the exception that proves the rule: it names the file, so it is the one setting that cannot be written in one.

There are **two files**, and which one you get is `COCCO_CONFIG`. `cocco.dev.toml` is **committed**: it is secret-free by construction, and the Justfile points at it. `cocco.toml` is gitignored and is a deployment's own; `cocco.example.toml` is its commented template. **A missing file is still not an error** — and now says more than it did, because `DEFAULT_DATABASE_URL` is the empty `postgresql+psycopg://`, which libpq resolves from `PGUSER`/`PGDATABASE`/`PGHOST`. That is deliberate and is what lets a file in git avoid naming the devShell's socket, whose path is `$PWD/.pg/sock`. It also means importing `cocco.main` needs no environment and no file at all, so the unit tests run outside the dev shell. A setting left in its old place — a `COCCO_DUMMY_AUTH` in the shell, a `secret_key` in the file — is **warned about, not ignored**: `_warn_about_removed_settings` names the old spelling and the new one, and only warns, because a stale variable must not stop the app from booting. `dummy_user` is the third kind and has its own sentence there: it did not move, it was **deleted**, so there is no "it is X now" to print — the answer is that `/login` asks. `[dev] dummy_auth` is what makes a fresh checkout usable at all, and `[auth] administrators` is where a deployment's first administrators are listed (a bootstrap — see "Authorization").

## Architecture

### Execution pipeline (`cocco/judge.py`)

Three layers, loosely coupled:

1. **`prewarm_runtimes` / `build_runtime` / `resolve_runtime(runtime_name, bin_name)`** — `build_runtime` shells out to `nix build <flake>#<runtime>` and returns the `/nix/store` paths; `resolve_runtime` is a **lookup in `_RUNTIME_CACHE` and nothing else**. Runtimes are whatever the flake exposes (`python3`, `gcc`, `jdk`). The full store is later bind-mounted into the sandbox via `--dir=/nix/store` so the interpreter/compiler can find its libraries.

   **The cache is filled once, at startup, and `nix` is never run while a submission is being graded.** `main()` calls `prewarm_runtimes(load_languages())` after the schema check and before `run_loop`, which is what its log line always claimed and never did. Resolution used to be lazy, per `(runtime, binary)` pair, on the first use of each language — so the first java upload paid for a JDK inside a graded run, and a flake that could not build one said so *then*. One build per **runtime**, not per binary: gcc answers for both `gcc` and `g++`, jdk for both `javac` and `java`. A `resolve_runtime` miss is therefore a `RuntimeUnavailable` and a bug — `languages.toml` is the single source both the prewarm and the lookup read, so the two can only disagree by mistake, and building a compiler halfway through somebody's submission is not a recovery.

   **Which flake is `[judge] flake`**, and it is the one setting the web app never reads. It used to be a literal `.#` — the flake in the *working directory* — which is a coupling the judge could not state and an installed `cocco-judge` could not satisfy, since a command on PATH has no checkout to be started from. The default is still `"."`, so nothing changes for a checkout; a deployment names an absolute path or a flake reference. It reaches `resolve_runtime` as the module-level `FLAKE_REF`, assigned once by `main()`, rather than as an argument threaded through `Language.run` → `run_submission` → `run_loop`: `Language` is a frozen dataclass built straight from `languages.toml` and has nowhere to carry it, and this is the same seam `main.DUMMY_AUTH` is. `languages.toml` itself stays deliberately *not* configurable — it is read out of the package by both processes, and pointing it elsewhere would only be one more way for them to disagree in silence.

   **`[judge] gcroots` is another setting only the judge reads (`[judge] concurrency`, the number of worker tasks, is the third), and it exists because a resolved runtime was collectable.** `build_runtime` passes `--out-link <gcroots>/<runtime>`, which is what makes nix register an *indirect GC root* under `/nix/var/nix/gcroots/auto`. It used to pass `--no-link`: the store path was then held by nothing but a `Path` in `_RUNTIME_CACHE`, which nix cannot see, so a `nix-store --gc` on the judge host was free to collect the very interpreter a running judge was about to bind-mount into a box — surfacing as submissions dying on a missing file, with nothing in the log to say why. The default is `$XDG_STATE_HOME/cocco/gcroots`, so nothing needs configuring; it is deliberately **not** derived from `[storage] root`, which is the one directory the two processes share and may well be on NFS, while a GC root is local to the machine whose store it protects. It reaches `build_runtime` as `GCROOTS_DIR`, the same module-level seam `FLAKE_REF` is, but initialised to the default rather than to None so that a test resolving a runtime without going through `main()` still roots what it builds. Two things worth knowing: the directory is **per judge host and per deployment** — two judges sharing it with different `flake` refs would overwrite each other's links, and a collection in between could take a live runtime — and the roots are **self-healing**, because nix prunes a dangling auto-root and every judge start re-registers all of them.

2. **`isolate_run(exe, stdin, args, ...)`** — the sandbox primitive. Acquires a box id from `_IDS_POOL` (a bounded `asyncio.Queue` of ids, so concurrent runs don't collide on isolate box numbers), runs `isolate --init`, copies any file/dir arguments into the box, runs `isolate --run --cg` with cgroup-based time and memory limits, parses the `--meta` file, then `isolate --cleanup`. Returns a `RunOutcome` with retcode, stdout, stderr, elapsed time, peak RSS, and the isolate status/message.

3. **`Language`** (loaded from `languages.toml` via `load_languages`) — glues the two together for each language. Three shapes, chosen by which of `compiler`/`interpreter` are set:
   - interpreted (e.g. python): `interpreter` only — the interpreter runs inside isolate with the source copied in.
   - compiled native (e.g. c, cpp): `compiler` only — compile outside isolate, run the binary inside.
   - compiled + VM (e.g. java): both — `javac` outside isolate, `java` inside, with `additional_processes` added to the sandbox process budget for the JVM.

   Compiler args support `{source}`, `{output}`, `{workdir}`; interpreter args support `{executable}`, `{workdir}`, `{name}`. When adding a language, edit `languages.toml` and, if a new runtime is needed, add a package output in `flake.nix`.

4. **`run_loop`**: `[judge] concurrency` worker tasks, plus `listen_for_work`. Each worker claims one job, runs it to the end and claims again. **A claim is `FOR UPDATE SKIP LOCKED`**, in `pull_queued_submission` and `pull_queued_exercise_job` alike. Without it, two claimers can take the same row, because the `UPDATE … WHERE id = (SELECT … LIMIT 1)` subquery is not re-evaluated when the second UPDATE waits on the first. Claims stay synchronous on the event loop: a claim is about a millisecond of blocking, and `SKIP LOCKED` never waits on a lock. The default concurrency is half the hardware threads the judge's CPU affinity allows. It is sized against the **CPU** because the CPU runs out first: a box is mostly an interpreter starting up, and on a 16-thread host the judge drained a queue about 9× faster with 8 workers and no faster with 16. The box pool (32) is what bounds the sandboxes running at once. Two judges on one host cannot share a database, because each numbers its boxes from 0; judges on separate hosts can.

   **Priority is `QUEUES`, a tuple asked in order every time a worker is free: exercise run > exercise check > exercise build > challenge submission.** The priority is strict, so a regrade of 300 submissions waits behind a live lesson. That is the point: a student staring at Run is the most latency-sensitive person in the building. Each entry is one claim on one queue rather than one SQL query for "the most urgent of anything", because the challenge queue is another table. **The web app wakes the judge with `pg_notify`** (`notify_queue`, channel `QUEUE_CHANNEL`), sent inside the transaction that inserts a submission, a regrade or an exercise job. PostgreSQL delivers it at commit and drops it on a rollback, so a wake-up never arrives ahead of the row it is about. `Wakeup` hands a worker its ticket *before* the worker looks at the queue, so a notification that lands between the look and the sleep is not lost. The 5 s poll stays as the fallback, and losing the listening connection costs latency and nothing else. A cancelled judge (`SIGTERM`, Ctrl-C) puts its half-done work back on the queue. A **hard-killed** one leaves rows `running`, deliberately unrecovered at startup: several judges can share the database, and one judge's startup cannot tell an orphan from another judge's work in progress.

### Evaluators (`cocco/evaluators/`, run from `cocco/judge.py`)

An evaluator is a **program, not a class**: course staff upload one per challenge and the judge runs it through the same isolate + Nix path as a submission, so a checker can be written in any language in `languages.toml`. The contract is

```
<script> <actual_output> <expected_output> <test_input>
```

three paths on argv, and **one JSON object on stdout**: `{"score": <float>, "message": "<optional>"}`. `parse_evaluator_output` is the only thing that reads it, and `EvaluatorResult` in `schema.py` is the contract — the score must be finite, because `Infinity`/`NaN` are Python spellings that no other language emits and would quietly make the contract Python-only. Extra keys are ignored, so the shape can grow.

**Exiting non-zero is never "wrong answer"** — it means the checker broke. Every way an evaluator can misbehave (non-zero exit, timeout, OOM, non-JSON output, a missing file, an unknown extension, a compile error) becomes `OutcomeEnum.evaluator_error`, deliberately not `judge_internal_error` (which means "file a bug" and carries a Python repr) and deliberately not routed through `_classify_isolate_failure`, which would report a *checker's* timeout as the *submission's* — a wrong verdict, and a bogus row on the strict `running_time` leaderboard. `prepare_evaluator` raises `EvaluatorError` for the failures that are not about any one test, and those fail every test case.

`challenge.evaluator_path` is NULL for a challenge that never chose a checker; the judge then grades with `cocco/evaluators/equality.py`, straight from the source tree and never copied into storage, so a fix there reaches every such challenge. The other bundled scripts (`diff.py`, `accuracy.py`, `f1.py`) are listed in `BUNDLED` and are what the editor's "start from a bundled evaluator" copies in; `bundled_path` refuses any name that is not a key, so the form field can never become a path. They are plain-stdlib Python with `evaluate()` split out of `__main__` so they are testable by import as well as by subprocess, and they double as the worked example a teacher edits.

The evaluator's sandbox budget is fixed in `judge.py` (`EVALUATOR_TIME_LIMIT_S`, `EVALUATOR_MEM_LIMIT_KB`) and is far more generous than a submission's: a checker legitimately does more work than the program it checks, and a teacher must not have to widen the *students'* limits to get theirs to finish. It is compiled once per submission and run once per test case, and the outcome's `elapsed_s`/`peak_memory_bytes` stay the submission's.

### Data model (`cocco/model.py`)

SQLAlchemy Core `Table`s on a shared `MetaData` form the domain: `User` (with `UserRole` via `Enrollment`, and `Administrator` beside it — every `UserRole` is a *per-course* thing, so the site-wide power to create a course is its own table, keyed on the email so it can name someone who has never signed in), `Course` → `Challenge` → `TestCase` (inputs/expected outputs referenced by filesystem path, not stored in the DB — see `DESIGN.md` "Test case inputs"; `is_sample` marks the ones students may download, `is_active` the ones new submissions are graded on), `Submission` → `TestOutcome`. There is no queue table: a submission is queued when its `status` is `SubmissionStatus.queued`. `Challenge.ranking` (a `LeaderboardRanking`) is presentation-only — it picks the leaderboard the challenge page shows and the judge never reads it. Beside that domain, and sharing nothing with it above the execution layer, are the six `exercise_*` tables; see "Exercises".

`User.blocked_at` / `blocked_reason` are the **blacklist**, and they are columns on the row rather than a table of their own — deliberately the opposite of `Administrator`, and for the opposite reason. That table is keyed on an address because it must name people who have never signed in; a block always names someone who already did something, so the account exists. Living on the row means `get_user` — which already reads it on every request — asks the question for free, and a block therefore takes effect on the blocked person's very next click. `_USER_COLUMNS` in `repository.py` is the one column list behind both `query_user` and `query_user_by_email`, spelled once because a list that carried `blocked_at` in one of them and not the other would let a blocked person in through whichever door forgot.

**Every timestamp is `timestamptz`, and Postgres writes all of them.** `created_at()` and
`updated_at()` at the top of `model.py` are the two shapes, factories rather than constants because a
`Column` belongs to one `Table`; between them they cover `user`, `course`, `enrollment`,
`challenge` and `exercise_set`, with `created_at` alone on `administrator`, `testcase` and
`exercise_set_version`, and `updated_at` alone on `exercise_session`, whose creation is its
`opened_at`. `datetime.now()` is not how a timestamp is written any more, anywhere — the web app
and the judge are two processes with two
clocks, so `submission.enqueued` and `submission.started_at` could disagree about which came first,
and `now()` is one clock and the *transaction's*. `updated_at` is a blanket `onupdate`, so it says
"this row changed" and nothing finer: a reorder bumps every challenge of a course and a sign-in bumps
the user. One write escapes `onupdate` and names the column itself — `set_enrollment_role`, because
SQLAlchemy fires it for an UPDATE and not for the DO UPDATE of an upsert.

Three of those columns answer a question `created_at` cannot. **`user.first_login_at` is not
`user.created_at`**, because `get_or_create_user` is reached both from the sign-in path and from the
course people page, where a teacher writes out a roster before term starts — so NULL there is
precisely "this account has never been near the login page", and `record_sign_in` sets it once
through a `coalesce` in the statement that writes `last_login_at`, called from `_complete_sign_in`
*after* the blacklist refusal. **`submission.started_at` / `finished_at`** bracket a judge's run
(`pull_queued_submission` and `mark_submission_done` stamp them) and are put back to NULL by
`regrade_challenge`, which returns the row to the queue. `submission.enqueued` keeps its name: it is
this table's `created_at` under a better one. Neither the new user columns nor the submission ones go
through `_USER_COLUMNS`, which stays the short list `schema.User` is built from.

`cocco/schema.py` holds the Pydantic mirrors used for validation and for the judge's `SubmissionRunData`. `schema.User` carries the two block fields **without defaults**: `get_user` refuses on them, so a query that selected a trimmed column list has to fail loudly at validation rather than quietly report everybody as welcome.

### Migrations (`cocco/migrate.py`, `cocco/migrations/`)

**`model.py` is the head of a sequence of revisions, and the schema is not created on boot.** Alembic
owns it: `cocco/migrations/versions/` holds the revisions, `cocco-migrate` moves a database between
them, and `cocco-web` and `cocco-judge` *check* and never migrate. Both call
`check_schema_is_current` before they serve anything and refuse to start against a database that is
behind, printing the command that fixes it — a server that quietly ran DDL would be one that could
migrate a deployment nobody meant to migrate, and the old `metadata.create_all` on startup was worse
than that: it created *missing tables*, never altered an existing one, and so silently papered over
a web app and a judge pointed at different databases.

That refusal is spelled **twice on the web side**, and the duplication is load-bearing. Uvicorn only
turns a failed startup into a non-zero exit when there is one worker and no reloader; under
`cocco-web --reload` the reload supervisor outlives the dead child, does not restart it and exits 0,
so a supervisor waiting on the process (a shell's `wait -n`, a systemd unit) would never see it fail and you would get a silently absent app. So `_check_schema`
in `cocco/server.py` asks *before uvicorn exists*, which is what makes the refusal a plain non-zero
exit under every combination of flags, and the copy in the startup hook covers every other ASGI
runner and makes each worker verify for itself. `cocco/server.py` keeps its lazy-import discipline
doing it: the check pulls in `cocco.config` and `cocco.migrate`, never `cocco.main`.

`env.py` reads `load_config(config_path())` itself rather than `config.get_main_option("sqlalchemy.url")`,
so `cocco-migrate` cannot migrate a database the two servers are not talking to. That is also the only
way cocco's URL survives at all: `alembic.config.Config` is a `ConfigParser` underneath, so a `%` in a
password would be an interpolation error and the empty `postgresql+psycopg://` has no business being
written down. `script_location` is the one main option ever set; a URL or a connection travels in
`config.attributes`, which is a plain dict — that is how `[dev] reset_db` and the drift test run
migrations on a connection they already hold.

**Three things autogenerate cannot see**, and each is a hand-written revision. A new *value* on an
existing enum produces no diff whatsoever (`ALTER TYPE outcomeenum ADD VALUE 'x'`, inside
`op.get_context().autocommit_block()`, because a value added in a transaction cannot be used until it
commits). A changed **server default** is invisible because `compare_server_default` is off in
`COMPARE_OPTIONS` — Postgres reads `challenge.ranking`'s back as `'running_time'::leaderboardranking`
against a model that says `text("'running_time'")`, so leaving it on would be permanent phantom drift.
That used to be a footnote about three columns; it is now the commonest hand edit there is, because
every timestamp in the schema carries `server_default=func.now()`, and the two of
them that *changed* (`api_token.created_at` and `submission.enqueued`, which traded a Python-side
`default=datetime.now` for it) were invisible to the autogenerate that wrote their own revision. The
lesson generalizes: a revision that adds a timestamp is autogenerated and then read, because the
column will be there and the default will not. And a **new PG ENUM type** is created implicitly by
`op.create_table` but never dropped by `op.drop_table`, so every revision that adds one owes its
`downgrade` an explicit drop — the baseline's `ENUM_TYPES` loop is the worked example, and a
`downgrade base` followed by an `upgrade head` is what proves it.

`cocco/test_migrations.py` is what stops the revisions and `model.py` drifting apart: it runs
`upgrade head` on a scratch Postgres and asserts `compare_metadata` finds nothing, on exactly the
`COMPARE_OPTIONS` `env.py` configures autogenerate with — the two spellings being one constant is why
it cannot pass on a diff a revision would record. It runs against **`cocco-test-migrations`**, its
own database, because it drops the schema on the way out and the rest of the suite builds one once
per session next door — pointed at the shared database it would strand whichever test happened to be
collected after it. Every other test now builds its schema from these same revisions, so a column
added to `model.py` and to no revision surfaces there too, as a query against a column that is not
there; this file stays the *direct* question rather than the inference, naming the drift instead of
leaving somebody to work it out from a failure three files away. `reset_schema` is `DROP SCHEMA public CASCADE`, not `metadata.drop_all`, and
that is the one trap worth knowing: `drop_all` takes the tables and the ENUM types but leaves
`alembic_version` standing, so the `upgrade head` after it finds a database already claiming to be at
head and does nothing — an empty database that *passes* the check and 500s on the first query.

### Queries (`cocco/repository.py`)

Every query lives here. Rather than an ORM with relationships, the page-level queries are raw SQL that build one nested JSON document with `json_agg` / `row_to_json` and return a plain dict or list — see `DESIGN.md` "Database design" for the rationale. Templates then walk that document with attribute syntax (Jinja falls back to `__getitem__`). New page queries should follow the same shape rather than issuing several round trips.

These queries are **Postgres-specific**. The judge-side helpers in the same module (`pull_queued_submission`, `insert_test_outcomes`, `mark_submission_done`) are plain Core and take a *sync* `Connection`; everything the web app uses is `async` and takes an `AsyncConnection`.

Insert helpers (`insert_enrollment`, `insert_submission`) deliberately do not commit — the caller owns the transaction. `get_db` in `main.py` yields a bare connection with no surrounding transaction, so a route that writes must `await db.commit()` itself.

### HTTP layer (`cocco/main.py`)

FastAPI app exposed as `cocco.main:app` (see `[tool.fastapi]` in `pyproject.toml`, and `cocco/server.py`, which is what `cocco-web` runs). Server-rendered Jinja templates in `cocco/templates/`, styled with Tailwind v4 + daisyUI v5 (`assets/styles.css` → `cocco/static/styles.css`), made live with htmx.

Pages return a full document; anything that needs to stay fresh is a fragment template named `f-*.jinja` served from a `/f/...` route and polled with `hx-get` + `hx-trigger="load, every Ns"`. That polling is what makes a submission visibly move from `queued` to `running` to a score without a page reload. A partial that is only ever `{% include %}`d, never served, gets a `_` prefix instead — `_statement.jinja` is the problem statement shared by the two challenge views.

One challenge URL, two pages: `get_challenge` renders `challenge-staff.jinja` for course staff and `challenge.jinja` for everyone else, and `?as=student` lets staff see what a student sees. The staff page drops the submit form and shows `query_challenge_standings` — the whole class, one row per student, each linking to `get_challenge_student` for that student's attempts.

A submission has its own page, `get_submission` at `/submissions/{id}` — flat, because the id already determines its challenge and course. It shows the source (highlighted by Pygments), the compiler's output when the submission failed to compile, and the per-test outcomes; the last two are the polled fragment `f-submission-results.jinja`, which uses `query_submission_outcomes` rather than the page's `query_submission` so a megabyte of source is not re-sent every two seconds. Both entry points the user reaches it from — the student's own history and the staff view of one student — are the same fragment, `f-submissions.jinja`, so the link is written once.

Uploads carry no language field: `post_submission` maps the file extension to a language through `EXTENSION_TO_LANGUAGE`, built from the same `languages.toml` the judge loads. Upload failures come back as **200 with an error alert**, not a 4xx, because htmx does not swap error responses by default. That rule is about htmx and stops where htmx does: the `/api` routes below refuse with a real status code and a JSON body, because a client that cannot tell a refusal from a success by its status is one that overwrites a challenge and reports that it worked.

The home page (`index`, `index-logged-in.jinja`) is the courses list, in two halves: the courses the
viewer is enrolled on — as staff or as a student, `query_courses` splits on `role IS NULL` — and
everything else. Both are `_course-card.jinja`, the counterpart of `_challenge-card.jinja` and
deliberately its twin, so the page reads as the same product as a course page. The filter box is
`q`, passed straight to `query_courses`, which matches it against the **title** with `position()`
rather than `ILIKE '%'||:q||'%'`: a substring test, so a title typed with a `%` or a `_` in it is
matched literally and there is no escaping to get wrong. It swaps `f-courses.jinja` — both halves,
so one request refreshes everything — and the input sits **outside** `#courses`, its own target,
because an input rebuilt on every keystroke loses focus. The page inlines that same fragment on
first paint.

The `Enroll!` button on a course the viewer is not on is `post_course_enroll`, and there are two of
them: on the home page's card, where it renders the courses fragment back — with the filter box's
`q` included, so enrolling under a filter keeps it — because the point of the click is that the
course *moves* from the bottom half to the top; and on the course preview, which has no list to
re-render, so that one sends `next` and gets a `204` + `HX-Redirect` back to the same course, now
opened up. `next` goes through `safe_next_path` like every other destination.

Its authorization is `CurrentUser` and deliberately nothing more: it is the way in, and a
membership gate here would lock the door from the inside. It is therefore **the only route under a
course a non-member may call**, which is why the row it writes has to be the one row it can write:
the role is fixed inside `insert_self_enrollment` rather than taken from the request, so no form
field can spell `teacher`, and the `ON CONFLICT DO NOTHING` there makes a double click a no-op
*and* guarantees an existing role is never overwritten, so a teacher who clicks it is not demoted
to student. There is no way back out: leaving a course is still a course-staff matter.

**`course.enrollment_key` is what makes that door selective.** NULL is a course anyone signed in
may join; a value is a shared secret course staff set in the editor and announce to the class, and
`post_course_enroll` refuses — with `hmac.compare_digest`, on the trimmed but otherwise exact
string — before writing anything. It is stored and shown **in plaintext**, deliberately: staff have
to read it back to say it out loud, which is the whole point of a key like this, and it guards a
class list rather than an account. A rule follows from that: the **course editor is the only place
it is ever printed**. `query_courses` and `query_course` report `enrollment_key IS NOT NULL` as
`requires_key` and list their columns rather than `SELECT *`, so the value cannot ride along into a
page served to the very people it is keeping out, and `query_enrollment_key` is the single read of
the column, which exists to compare it. The field itself lives only on the course preview — a
wrong-key message has nowhere to go inside a home-page card, so a key-protected course's card
offers `Enroll…`, a link there instead. The preview and the refusal are the same fragment,
`f-enroll.jinja`, inlined on first paint and swapped back with the message on a bad key, so what a
refused student is looking at is the form they just typed into.

Courses are created at `/courses/new` (`course-new.jinja`), reached from a button the home page
shows only to an administrator; the form is the title and description halves of the course editor,
and on success it answers `204` + `HX-Redirect` to the new course's editor, the way
`post_challenge_new` lands on the challenge editor. Like the editor below, it is **declared above
`get_course`** — `/courses/{course_id}` would otherwise swallow `/courses/new`.

`/admin` (`admin.jinja`) is the site's own page, reached from a second button beside that one and
the only other thing behind `Admin`. Unlike `/courses/new` its position among the routes is *not*
load-bearing: `/admin` is a literal segment that collides with no path parameter. It has **one swap
target**, `#users`, and every route on it — the filter box's GET and all three mutations — answers
with the same `f-users.jinja`, alert included: that is `post_course_enroll` answering with the list
it changed, with `f-enroll.jinja`'s rule folded in, and it means the sentence and the new state
arrive in one swap instead of two that have to be kept in step. Both the filter box **and the
appoint form sit outside `#users`**. For the filter that is the rule the home page already states,
an input rebuilt on every keystroke loses focus; for the appoint form it is one step further —
the filter re-renders that div on *every keystroke*, so a form inside it would have a half-typed
address wiped by somebody typing in the box above. Every mutation carries `q`, so acting under a
filter leaves the filter in force. `post_administrator`'s `email` is a **defaulted** `Form` field
rather than a required one, because a missing field is a `422` and htmx drops the body of an error
response — the person who submitted an empty box would see nothing at all. Blocking is an inline
per-row form with a reason box, not the `<dialog class="modal">` `challenge-staff.jinja` uses for
a regrade: that precedent is for an irreversible action, blocking is undone by the button that
replaces it, and N dialogs with N unique ids would be rebuilt on every keystroke in the filter.

The course has an editor of its own at `/courses/{course_id}/edit` — the course's title and description (the same markdown textarea and `post_markdown_preview` the challenge form uses) plus the challenge list, reorderable with ↑/↓. It is **declared above `get_challenge`**, and that is load-bearing: `/courses/{course_id}/{challenge_id}` matches any single segment, so a `/courses/1/edit` declared after it would route there and be rejected as an unparseable id. The cards themselves are `_challenge-card.jinja`, shared by the course page and by `f-course-challenges.jinja` — the fragment every move swaps in — so the student's list and the editor's cannot drift apart.

**Both editors end in a "Danger zone" card**, and it holds the two ends of an object's life:
archiving it, which is reversible, and deleting it, which is not. The course's is
administrators only — `Admin`, and deliberately *not* `CourseEditor`, which lets a plain teacher
on to that page; what the home page lists and what exists at all is the site's business rather
than one course's, the same line `post_course_new` draws at the other end, and the reason
`require_admin`'s sentence names the permission rather than any of the three places it is now
used. `get_course_edit` passes `is_admin` down so the card can be drawn or not; the routes behind
it check for themselves. The challenge's needs no such flag — the whole page is behind
`ChallengeEditor`, which already *is* "administrators and course staff", and which keeps the
`challenge_in_course` cross-check that stops staff of one course naming their own in front of
somebody else's challenge. The three course routes are **declared above `get_challenge`**, for the
editor's reason; the challenge's have a third segment and cannot collide. Archive and unarchive
are two routes rather than one with a flag, mirroring `/admin/users/{id}/block` and `/unblock`,
and every one of the six answers `204` + `HX-Redirect` — back to the same editor for an archive,
because the badge, every challenge card and the button's own label all change; to the course
editor for a deleted challenge, which is the list it has just left; to the home page for a deleted
course, which has no page left to swap into.

**A delete asks for the thing's own title, typed out.** That is the regrade dialog's precedent one
turn stricter, because a regrade can be run again and this cannot: `<dialog class="modal">` with a
text box, and an inline script that only *enables* the button on an exact match.
`_confirmation_matches` compares the same string again in the route, because a disabled control is
presentation and never the check — the rule `require_challenge_editor` states for hidden buttons.
A plain `==` on the trimmed string and not `hmac.compare_digest`, unlike the enrollment key: the
title is printed on the page the box sits on, so this confirms an intention rather than proving a
secret. The title reaches the script through **`data-expected`**, through Jinja's attribute
escaping, never interpolated into a JS string literal where a course called `Dijkstra's` would end
the string. Both danger zones refuse — 200 with `f-edit-result.jinja`, htmx's rule — on a wrong
name and while a judge is mid-run; only the success is a 204.

**Archiving is a visibility state, not a lock**, and that is what keeps the whole feature down to
**three enforcement points**: `post_submission`, `post_course_enroll`, and the routes that queue an
exercise Run or Check (`_post_session_job`, `_post_exercise_preview_job`; see "Exercises"). An
exercise *build* is not refused, because importing is editing. Hiding a challenge is the
one that *is* a lock, and it is enforced somewhere else entirely — in the two dependencies, not in
any route; see "Visible is `challenge.visible_from`" below. An archived course or
challenge stays readable for the people already enrolled — statement, their attempts, scores,
leaderboards — stops taking submissions, leaves everybody else's listing, and admits nobody new.
Staff go on editing it, and unarchive it from where they archived it; so does `cocco push`, since
the API is the editor by another route. `archived_at` is in the baseline revision, so it reaches a
database the way every column now does — `cocco-migrate upgrade head`.

Beside it, `/courses/{course_id}/people` (`course-people.jinja`) is the class list: who is
enrolled, in what role, how much they have submitted *here*, and the three ways staff change it.
It is `/admin` reshaped for one course and keeps that page's rules — one swap target, `#people`;
every route answering with `f-course-people.jinja`, alert folded in; every mutation carrying `q`;
both the enrol form and the filter box **outside** the target. It is **declared above
`get_challenge`** for the same load-bearing reason the editor is. Course staff only, through
`CourseEditor` — including its *reads*, because a roster is a list of real people who are on one
course and not another.

Enrolling takes an **address**, not a user id, and this is the second (and last) place a human
types one: a teacher writes out a roster before term starts, so most of those people have not
signed in. Unlike `administrator`, `enrollment` is keyed on `user.id` and cannot name an address
with no account, so the account is **created** here, through the same `get_or_create_user` the
provider's callback uses. Two consequences worth knowing. A pre-enrolled person gets a real `user`
row, so `/admin`'s `accounts` list shows them indistinguishably from somebody who has actually
signed in — there is no `pending_*` half here, because there is nothing pending. And a *blocked*
account is deliberately **not** refused, the opposite of appointing an administrator: that is
one-way through the app and so has to refuse, while this is undone by the button in the row it
creates, and a block stops a person without retracting that they are on the course.

Two rules the page will not break, and they sit where they cannot be raced. **You cannot remove
yourself** — checked in the route, because it is not a race and deserves its own sentence: it
would shut the door on the page you are standing on. **A course never loses its last teacher** —
checked inside `set_enrollment_role`'s `ON CONFLICT` clause and `delete_enrollment`'s `WHERE`, the
way `block_user` refuses an administrator, and only sound because every mutation takes
`lock_course` first: `FOR UPDATE` on the course row serializes them, and under READ COMMITTED two
concurrent statements would otherwise each see the other's teacher and both succeed. `Remove` on
somebody who has submitted asks for an `hx-confirm` first — not the `<dialog>` a regrade opens,
because nothing here is destroyed and N dialogs would be rebuilt on every keystroke in the filter.

`/users/{user_id}` (`profile.jinja`) is the account itself, and it is **one URL, two pages** the
way the challenge is: the owner gets forms, a site administrator gets the same facts as text.
There are two dependencies rather than one, and the asymmetry is the point.
`require_profile_reader` allows the owner or an administrator and **returns `can_edit`**, so the
route asks once and the template does not work it out again — the trick `authorize_submission`
plays with `is_staff`. `require_profile_owner` has no administrator branch at all: reading a
profile is oversight, renaming somebody is impersonation, and `/admin`'s business is what an
account may *do* — appoint it, block it — not what it is called. Course staff are strangers here
even to their own students, because the page spans every course rather than the one in a URL.
The address is nobody's to edit, including its owner's: it is the login identifier a provider proves.
`update_user` is the write, deliberately not the path a block takes — `block_user` has a rule in
its `WHERE` clause that going through the kwargs helper would lose.

Everything a mutation can change is one swap target, `#identity`, alert included, which is
`/admin`'s rule again; the address, the badges and the course list sit outside it because no
button touches them, and dragging them in would drag the Postgres-only `query_user_profile` in
behind — the mutations answer from `query_user`, which is plain Core. The navbar shows the same
name and face, so a rename swaps a fresh `_navbar-identity.jinja` **out of band**: the partial
exists so those two copies cannot drift, and right after a rename is exactly when a drift would be
noticed. There are two ways in, the dropdown's `Profile` and the nickname in `f-users.jinja`.

**The avatar is `user.image`, a blob on the row, and `sniff_image_mime` decides its type.** Never
the upload's own `content_type`, which is written by whoever is uploading: `user_avatar` serves
those bytes straight back from this origin, so believing that header would be believing a stranger
about what the browser should run. SVG is refused for the same reason even though the identicon
*is* one — we generate identicons, we do not generate uploads — and the route answers with
`nosniff` and a `default-src 'none'` CSP as the second lock. It also sets `Cache-Control:
no-cache`, because the URL does not change when the picture does. `Generate from my name` reseeds
`generate_identicon` on the nickname **as it is now**, which is how a face catches up with a
rename: a rename never silently redraws one, since once somebody has uploaded a picture it is
theirs.

### Authentication (`cocco/auth.py`)

Login is **somebody else's login page**, and there are two of them: Google OIDC and GitHub OAuth2.
`GET /login` offers one button per *configured* provider; `GET /auth/{provider}` hands off to
Authlib and `GET /auth/{provider}/callback` comes back — **one parameterized pair of routes, not
one pair each**, because everything either provider does is the same sentence apart from the step
that turns a token into an address. There are no passwords and no sign-up step — **the account is
created the first time an accepted address comes back** — and it is keyed on the address alone, so
someone who signs in with Google today and GitHub tomorrow lands on the same account by
construction rather than by a rule anybody had to write. A new user has no enrollments and is
therefore read-only everywhere until course staff enroll them.

**A provider is enabled by its own two environment variables and by nothing else.**
`COCCO_GOOGLE_CLIENT_ID`/`_SECRET` and `COCCO_GITHUB_CLIENT_ID`/`_SECRET`, read by one
`_oauth_credentials` in `config.py`; either half alone is a typo, warned about, and that provider
stays off. There is no file spelling and therefore no `enabled = true` to disagree with the
credentials — `AuthConfig.google` / `.github` being non-None *is* "configured", `oauth.register`
runs only for those, and `oauth_client(name)` answers None for the rest, which is the 503 branch.

**What differs between the two is one function**, and that is the whole of `OAUTH_PROVIDERS`: a
`_OAuthProvider` carries the URL segment, the label every sentence and button prints, and the
`email` resolver. `_google_email` reads the ID token Authlib has already verified (`userinfo`,
plus the `email_verified` check matched against `True`/`"true"`, because the string `"false"` is
truthy). `_github_email` has to **ask** — GitHub has no ID token, so it calls `GET /user/emails`,
the only network request signing in makes on its own. The two failures are one exception,
`_NoProvedAddress`, carrying the sentence and the status, so the callback keeps a single `except`.
The `hd` hint is the one other branch on the provider name, in `get_auth_provider`.

**GitHub's address rule.** An account holds several addresses, so the **unverified are dropped
outright** — they prove nothing — and among the rest the first that passes `allowed_domains` wins,
the primary considered first. Deliberately not "the primary or nothing": a student's primary is
usually personal with the institutional address verified beside it, and refusing that would refuse
the people this is for. When none of them passes, the primary is returned anyway so that
`_complete_sign_in` prints its "not an institutional address" sentence, which names the domains —
a second, vaguer refusal for the same event would be a difference nobody could explain.

**The gate is `is_allowed_email`, on whatever address the provider vouched for.** A configured
domain admits itself and its subdomains, so `unipd.it` already covers `@studenti.unipd.it` and
`@phd.unipd.it`; the suffix test is on a dot boundary, so `notunipd.it` does not sneak in, and an
**empty allowlist admits nobody** — a deleted line in `cocco.toml` should lock the door, not open
it. Google's `hd` parameter is passed as a hint to the account chooser when `[auth] google_hd` is
set and is **never** the check: it is absent for consumer accounts, and it is not the identity.
A verified address is required before it is trusted at all, by both providers.

Every failure — a denied consent, a mismatched `state`, an unverified address, a domain that is
not ours, missing credentials, an unreachable provider — renders `login.jinja` with one sentence.
The callback is reached by a browser coming back from the provider, so a traceback there is a dead
end. An **unknown** provider segment is a bare **404** (`_oauth_provider`), `post_auth_dummy`'s
rule: a path this app does not serve should not admit that it exists.

`cocco/auth.py` never touches the network and does not know which provider asked: it is
`is_allowed_email`, `safe_next_path`, `nickname_from_email`, `oauth_redirect_uri` (one function
for both, so the two can only differ in the segment naming them) and `get_or_create_user`. Authlib
and the routes live in `main.py`, which is what keeps the interesting half unit-testable —
`test_auth_routes.py` replaces the whole of both providers with `_FakeOAuthClient`, three methods
including the `get` that is all of GitHub's API. The configuration is not here either: it is
`cocco/config.py`, because the judge reads the same file and a database URL is none of sign-in's
business. The dependency runs one way — `config.py` imports `is_allowed_email` and
`normalize_administrators` from here, and nothing here imports `config.py`.

The session is a signed cookie (Starlette's `SessionMiddleware`) holding nothing but `user_id`,
so a renamed or deleted account takes effect on the next request. `same_site` must stay `"lax"`:
the return from a provider is a top-level cross-site navigation, and under `"strict"` the cookie
carrying the `state` would not reach the callback, so every sign-in would fail as a state
mismatch. `_prune_stale_oauth_state` drops every `_state_*` entry rather than one provider's,
since only one flow can be in flight.

**`[auth] password_login` is the third real way in, and its default is the design.** An address
and a password, for the deployment neither provider can serve; off unless the file says otherwise,
and when it is off the feature *does not exist* — `_require_password_login` answers **404**, which
is `post_auth_dummy`'s rule, because hiding a form is presentation and never the check. A provider
proves that someone owns the mailbox; a password proves nothing about the address, and cocco has no mail
path anywhere in it, so it cannot send a verification link. Registration is therefore a **claim**,
and `claim_user_password` narrows it as far as it can be narrowed without email — a single
conditional INSERT whose `WHERE` is `first_login_at IS NULL`, with `ON CONFLICT DO NOTHING`, so the
rule is in the statement rather than in a check before it, the way `block_user` refuses an
administrator. `first_login_at` means exactly "this account has never been near the login page", so
a roster a teacher wrote out before term is claimable by its owner and an account somebody has
actually used never is. What remains — whoever types an unused institutional address first gets it
— is what a deployment accepts when it turns this on, and is why the default is false and why
`cocco.example.toml` spends a screen on it.

Three consequences. **`_complete_sign_in` stays the single funnel**, and registration is a
`claim_password=` keyword on it rather than a route with its own opinions: the allowlist is checked
*before* `get_or_create_user` writes anything, so an address outside `allowed_domains` never
becomes a row, and the blacklist refusal still runs afterwards. **Every way a sign-in can be wrong
is one sentence** — `_BAD_CREDENTIALS`, one module constant — because an unknown address, an
account with no password and a wrong password are the same event from outside; `verify_password`
takes `str | None` and turns None into False rather than short-circuiting, so they cost the same
work too, and a route that returned early for an address nobody has would be a way to read a class
list off the login page. Registration is the opposite and deliberately so: it is a form somebody is
filling in, so it says which field is wrong. And **scrypt runs in a threadpool** — 32 MiB of
sequential work on the event loop would stop every polled fragment in the app for its duration.

`cocco/passwords.py` is `tokens.py`'s deliberate opposite, and that file's `hash_token` already
says why: a token is 256 bits of `secrets`, so SHA-256 is right for it; a password is something a
person chose, so it needs a salt per password and a work factor. Stdlib `hashlib.scrypt`, no new
dependency, and `maxmem` passed explicitly because its 32 MiB default is exactly what `N=2**15,
r=8` needs plus overhead. The encoding `scrypt$n$r$p$salt$key` carries its own parameters, so
raising them later leaves every stored password working and `needs_rehash` upgrades each one the
next time its owner types it. **The hash lives in `user_password`, not on `user`**, for one reason
above the others: `_USER_COLUMNS` is read on every single request, and a credential has no business
travelling with the identity every route already holds. Keyed on `user.id` for `api_token`'s
reason — the account always exists — and no row means no password, which is what a
single-sign-on-only account looks like.

There are two places a password is set and they are not variations on one route. The **owner** sets
theirs at `/users/{id}/password`, behind `ProfileOwner` and never `ProfileReader` — that page
already draws the line between oversight and impersonation, and setting somebody's password is the
far side of it — and the current one is required whenever there is one to replace, because a
session left open on a shared machine is the ordinary way an account is taken. A **site
administrator** issues one from `/admin`, generated rather than chosen and shown exactly once,
`f-tokens.jinja`'s discipline; that is the whole answer to a forgotten password and the reason no
SMTP setting exists anywhere. It leaves `first_login_at` alone, so it never re-opens an account to
somebody else's claim. There is **no rate limiting** anywhere in this app, here included; that
belongs at the reverse proxy and `cocco.example.toml` says so.

**`[dev] dummy_auth` is a sign-in, not a bypass, and that is the whole of its design.** It puts a
text box on `/login`; `post_auth_dummy` takes the address typed into it and mints an ordinary
session, so everything downstream — the cookie, `get_user`, the block — is the **one** code path
Google's callback also lands on. It replaced a shortcut that signed every request in as an address
named in the file (`[dev] dummy_user`, now removed and warned about in both its spellings), which
made `get_user` two functions and meant seeing what a student sees was an edit and a restart — on
an app whose every permission is per course, so hopping between the seeded cast is the commonest
thing one does while developing it. Setting it on a public deployment is equivalent to publishing
every account's password.

Three consequences follow. The box **reuses the real rules**: `_complete_sign_in` holds the
allowlist check, `get_or_create_user` and the blacklist refusal, and the Google callback calls the
same function — a rule the browser enforced and the box did not would be a difference nobody could
explain, and the box is the configuration all of this gets developed in. `post_auth_dummy`
answers **404** when the switch is off, not 403, so a deployment does not admit the route exists;
hiding the form is presentation and never the check. And `/logout` now goes somewhere: it lands on
the box, which is how you become somebody else.

The seeded cast is `matteo.ceccarello@unipd.it` and `alice@unipd.it` (`teacher` on every course)
and `bob@unipd.it` and `charlie@unipd.it` (students); `_login_page` passes those names to the
template so the box can print them and they cannot drift from `_populate_dummy_data`. Matteo and
Alice differ in one thing only: he is a **site administrator**, a row in `administrator` rather
than a role anywhere, which is why a fresh checkout can create a course. `create_db_and_tables`
seeds him whenever dummy auth is on — a file every checkout shares should not name one person as
the administrator of all of them, so `cocco.dev.toml` deliberately lists none and this is what
keeps the checkout usable — and seeds only him, so typing `alice@unipd.it` still shows what plain
course staff may do instead of quietly promoting her.

`main.DUMMY_AUTH` is derived from `CONFIG.dev` **once, at import, and stays a module-level name** —
`post_auth_dummy`, `_login_page` and `create_db_and_tables` read it rather than `CONFIG.dev`,
because that is the seam six test files flip. A frozen dataclass field has no equivalent, so this
is not a tidy-up waiting to happen.

### Authorization

`role` lives on `enrollment`, so permissions are **per course**, not site-wide. `EDITOR_ROLES`
(teacher, collaborator) is course staff; students may read the course and submit to it. The one
site-wide thing is the **administrator**, and it is deliberately not a `UserRole` — see below.

**Enrollment is the gate on a course's contents, not a label on them.** The unenrolled are not
read-only, they are shut out: `require_course_member` 403s anyone with no role on the course in the
path, `require_challenge_viewer` adds the `challenge_in_course` cross-check to it exactly as
`require_challenge_editor` does — otherwise a student of one course could name their own course in
front of another course's challenge — and `require_challenge_member` is the same check for the
three routes whose URL carries only a challenge (`/f/submissions/{id}`, `/f/leaderboard/{id}`, the
sample download). Keying those on the challenge is not the mistake the editor check avoids: with
one id in the path there is no second key to disagree with. `EDITOR_ROLES` is a subset of
"enrolled", so staff pass the membership check without a second one — but staff of *one* course are
strangers on every other.

There are therefore **two doors into a course**, and they are not variations on one route.
`post_course_enroll` is the one a student walks through themselves, which is why the role is fixed
inside `insert_self_enrollment` and no form field can spell `teacher`. The people page is the one
course staff open for somebody else, and being behind `CourseEditor` is exactly what lets it name
any role — including `teacher` — and take an enrollment away again. `set_enrollment_role` is the
only write that does both; `insert_enrollment` survives for `post_course_new`'s single row.

Four things are deliberately outside the gate, and each would break something if it were not: the
home page and `/f/courses`, which must list every course or nobody could find one to enroll on;
`post_course_enroll`, the way in; and `get_course`, which for a non-member renders a **preview** —
the title, the description and the `Enroll!` button, and nothing of the contents. The preview is
not merely a template that hides things: `get_course` branches before the query, so
`query_course_info` never runs for someone who may not see its result.

Refusals are pages, not JSON. `Forbidden` is an `HTTPException` subclass carrying the sentence to
show, and `handle_forbidden` renders `403.jinja` through `base.jinja`; the handler is registered on
that subclass alone so FastAPI's defaults still answer everything else. It needs the viewer for the
navbar and an exception handler cannot ask for a dependency, so `get_user` stashes
`request.state.user`. Polled fragments are unaffected — htmx does not swap the body of an error
response.

The one exception is **creating a course**, the only permission that names no course, because the
course does not exist yet — which is exactly why it is not a role. A **site administrator** is a
row in the `administrator` table, keyed on the *email*: `cocco.toml` names the first set of them,
and an address has to be nameable before its owner has ever signed in, since the account only
appears on the first accepted callback. `is_administrator` is the single read of that table and
`require_admin` (the `Admin` dependency) 403s everyone else. The creator is still enrolled on the
new course as `teacher`, which is what course staff *means* there and what the editor they are
redirected to checks.

`[auth] administrators` is a **bootstrap, not the truth**: `create_db_and_tables` replays it into
the table on every boot with `ON CONFLICT DO NOTHING`, because a brand new deployment has nobody
to appoint its first administrator. It only ever adds — deleting the line demotes nobody, and
removing an administrator is a `DELETE` on the table.

**`/admin` is the second thing `Admin` guards**, which is why `require_admin`'s sentence names the
permission ("Only a site administrator can do that.") rather than either of the two places it is
used. The page lists everyone, appoints administrators and blocks accounts. Appointing takes an
**address**, not a user id — that is what the table holds, and naming somebody before their first
sign-in is the point of it. It is one of the **two** places in the app where a human types an
address (the other is the course's people page, below), so it is one of the two that call
`normalize_email` and refuse an address outside `allowed_domains`. `_load_auth_config` merely
*warns* about the same thing, because a bad line in `cocco.toml` must not stop the app from
booting; a form has somebody in front of it who can fix the typo. **There is no revoke**: removing
an administrator is still a `DELETE`.

That absence is load-bearing, and two guards follow from it. **An administrator is never blocked**
— enforced inside `block_user`'s `WHERE` clause, not by a check before the write, so an
appointment cannot land between the two — and **a blocked address is never appointed**. Either one
alone would leave a way to make an administrator who cannot sign in and cannot be unmade. It is
also what keeps a development checkout safe: the seeded administrator is the address a developer types into the
sign-in box, and blocking him is how a checkout locks itself out. The two
escape hatches belong side by side: `DELETE FROM administrator WHERE email = '…';` and
`UPDATE "user" SET blocked_at = NULL WHERE email = '…';`

**The block is enforced in `get_user`** — which has one branch, since dummy auth is a session like
any other — and again in `_complete_sign_in`, so a blocked person gets a sentence on the page they
are looking at instead of a session that dies on the first click. That second check is shared by
both ways in, Google's callback and the development box, which is what stops dummy auth being the
single configuration in which the feature does not exist — the configuration it gets developed in.
`Blocked` is a `Forbidden` subclass, so it stays a page and would degrade to `403.jinja` if its
handler were ever removed; `handle_blocked` renders `blocked.jinja` with **no viewer at all**,
because the navbar's avatar is itself behind `CurrentUser` and would 403 into a broken image on the
very page explaining the block — which is why `get_user` refuses *before* it stashes
`request.state.user`. The session is **not** cleared: that would bounce every later request to a
login page that explains nothing. Polled fragments simply freeze and poll into 403s, exactly as
they do for a student unenrolled mid-session; deliberately not the `HX-Redirect`
`handle_not_authenticated` sends, which would conflate being blocked with being signed out.

**An administrator is course staff on every course**, enrolled or not. That lives in exactly one
place: `effective_course_role` (and `effective_challenge_role`, for the routes whose URL carries
only a challenge) returns the enrollment's role, or `teacher` when the asker is an administrator —
`teacher` because that is already this app's spelling of full staff on a course, the very row
`post_course_new` writes. Every dependency resolves the role through it, so `EDITOR_ROLES` stays
the *per-course* notion and never has to learn about administrators, and every `role in
EDITOR_ROLES` downstream keeps working untouched. The lookup only runs when the enrollment did not
already answer the question, so course staff pay nothing for it. Two consequences worth knowing:
`get_course` gives an administrator the course itself rather than the enroll preview, and
`get_challenge` takes `can_edit` from the dependency rather than from `query_challenge`'s
`viewer_role`, which is a `LEFT JOIN` on `enrollment` and would be NULL for them.

Nothing is reachable without an identity: every route depends on `CurrentUser` or on `ApiUser`,
directly or through `require_challenge_editor` / `require_api_challenge_editor`, and the only ways
in are `/login`, `/auth/{provider}`, `/auth/{provider}/callback` and `/static`. For a browser that identity
is always the session; **`ApiUser` is the one place a request identifies itself any other way**, and
it exists because a command line has no browser to run OIDC in — see "The transfer API" below.

The challenge editor's routes all depend on `require_challenge_editor`, which 403s a non-staff user.
Hiding a control in a template is presentation, never access control — anything that mutates a
challenge must go through that dependency. The staff view's routes depend on it too, and there it
guards *reads*: the standings and the per-student page show one student's work to someone else.

The submission page is the exception to the dependency: a student must be able to open their own
work, so `get_submission` and its fragment call `authorize_submission` instead, which allows the
author and otherwise requires `EDITOR_ROLES`. It needs no membership check of its own — the author
was enrolled when they submitted, because `post_submission` is behind the gate too.

### The transfer API (`/api`, `cocco/transfer.py`, `cocco/cli.py`)

A challenge is otherwise only reachable one HTTP form at a time: an upload per test case, an
arrow-press per reorder, a `<textarea>` for the statement. **The API makes a challenge a file** —
`cocco pull 12`, edit, `cocco push 12` — and the ergonomic point is that reordering test cases
becomes moving two lines in a text editor and the statement becomes a markdown file you can keep in
git.

The zip is flat and **mirrors what is already on disk**: `challenge.toml`, `README.md` (the
statement, verbatim), `tests/`, and `evaluator/` when the challenge has an uploaded checker. Test
files live in one directory per challenge with no per-test subdirectory, which is why input and
output names are already unique within a challenge — `tests/` keeps exactly that shape, and that is
why **a test case is identified by its input file name**. The manifest carries no ids on purpose: a
directory is meant to be edited, copied between checkouts and committed, so renaming an input
*replaces* the test case rather than renaming it. Three things are deliberately absent from it.
`position` belongs to the course editor, and a key that silently did nothing would be worse than no
key; `course_id` and `challenge_id`, because the challenge a push lands on is the one in the URL,
never a number inside a file somebody may have copied; and `execution_order`, because the block
order *is* the order.

**`cocco/transfer.py` touches nothing** — no FastAPI, no `AsyncConnection`, no `Storage` — which is
the seam that makes the format testable by import, the way `auth.py` keeps the interesting half of
sign-in unit-testable. It owns the shape (`parse_manifest`, `dump_manifest`) and the one rule that
must not be got wrong (`plan_testcases`); `main.py` owns the I/O. Written with `toml.dumps` and read
with stdlib `tomllib`: hand-rolled escaping works until somebody names a challenge "Dijkstra's".
Every path is checked against `safe_filename` and its directory *before anything is opened* — a
`../`, an absolute path or a nested `tests/a/b.in` is a `ManifestError`, and `Storage.resolve` stays
the backstop rather than the check.

**A push can never strand a score**, which is the same rule `post_testcase_delete` enforces and the
reason `plan_testcases` sorts an absent test case into `retired` or `deleted` by `n_outcomes` alone:
graded means retired (row, files and outcomes all kept, `is_active` cleared), never graded means
deleted. A retired case *keeps its two files*, so a push that hands one of those names to a
different test case is refused before anything is written. Every other refusal happens up front too
— the manifest, the limits, the evaluator's language — so a rejected push leaves the challenge
exactly as it was. `execution_order` is then rewritten densely from the block order through negative
sentinels, for the reason `reorder_challenges` uses them.

The two ends share their rules rather than restating them. `_challenge_fields` validates the limits
and the ranking here as it does for the create and the edit form — this is its third caller, not a
second opinion — and `_check_evaluator_source` is the size/extension/UTF-8 chain, factored out of
`post_evaluator`, because a script the browser accepted and the CLI refused would be a difference
nobody could explain.

**A pulled directory remembers where it came from, and a push is conditional on it.** `cocco pull`
writes `.cocco/state.toml` — the server's URL, the challenge id, the version it pulled and a
fingerprint per file — and a `.gitignore` of `*` beside it, because that version is what *this*
checkout agreed with the server on and a colleague's clone has agreed with nothing. The version is
the export's `ETag` and goes back as the push's `If-Match`, so a challenge edited on the web since
is a **412** where it used to be silently overwritten; an unchanged one answers a pull's
`If-None-Match` with a 304. It is **derived, never stored**: `_challenge_version` digests the
manifest, the statement and each stored file's *size and mtime*, because a revision column would
have to be bumped by every route that touches a challenge or its files and the one that forgot
would be exactly the overwrite this exists to stop — and mtime rather than content, because gzip's
header timestamp makes identical plaintext differ on disk and reading every test case for a
precondition costs what the export does. A file rewritten with the same bytes therefore reads as a
change, which costs one pull that finds nothing to merge. A pull into a directory with state is a
**three-way comparison per file** (`reconcile` in `cli.py`): changed only on the server is taken,
changed only here is kept, changed on both is refused — or with `--force` taken after the local
copy is moved into `.cocco/discarded/` — and a file the server no longer has is removed, which is
what stops a stale `tests/` file breaking the next push. `challenge.toml` is fingerprinted as the
manifest it *means*, `dump_manifest(parse_manifest(…))`, since the server writes it its own way and
a byte comparison would make every hand-edited manifest a conflict. The URL in the state is
**compared and never used**: a token goes wherever the URL points and a directory is something
people hand around, so a mismatch is refused naming the `--url` to pass. `If-Match` stays optional
on the server — that is `cocco push --force`, and a zip pushed by hand — and the check is not in
the push's transaction, so a web edit landing inside that one request is still lost.

**Authentication is `ApiUser`, and it is the one identity in the app that is not a cookie.**
`get_api_user` tries `Authorization: Bearer`, then the session — that last so a signed-in browser
can follow a plain link to an export URL, and safe on the mutating half for the reason every other
POST here is, `same_site="lax"`. There is deliberately **no `DUMMY_AUTH` branch**, and there was
one: it came first and signed every API call in as the configured address, which meant a bearer
token presented to a development server was silently *ignored* — the one configuration in which the
header this function is mostly about did nothing. The cost is that `cocco pull` against a development server
now needs a token like anywhere else: mint one on the profile page and put it in `COCCO_TOKEN`. A
refusal is `ApiNotAuthenticated`, a plain `HTTPException(401)`, deliberately **not**
`NotAuthenticated`: that class is not an `HTTPException` precisely so its handler can choose between
a 303 and an `HX-Redirect`, and a CLI following the 303 would be handed a login page with a 200 on
it. `require_api_challenge_editor` is keyed on the challenge alone — with one id in the path there
is no second key to disagree with, `require_challenge_member`'s argument — and resolves through
`effective_challenge_role`, so an administrator is staff here as everywhere.

**A token is stored only as a hash** (`cocco/tokens.py`: `mint_token`, `hash_token`, `parse_bearer`),
so it is shown exactly once, when it is minted, because there is nowhere it could be read back from.
`api_token` is keyed on `user.id` rather than the address — the opposite of `Administrator` and for
the opposite reason: a token is minted on its owner's own profile page, so the account always
exists. `query_api_token_user` goes through `_USER_COLUMNS`, because that constant exists so that a
door which forgot `blocked_at` cannot be the one somebody walks through, and this is the third door.
The tokens section of `/users/{id}` is behind `ProfileOwner` and **not** `ProfileReader`: that page
already draws the line between oversight and impersonation, and listing somebody's credentials is on
the far side of it — an administrator sees no section at all. `last_used_at` is written on every
accepted call, which makes a GET write, and is the only thing that can tell an owner a token has
gone stale before they revoke it.

`cocco/cli.py` is `argparse` over `httpx`, run as `cocco` — `uv run cocco` in the checkout, plain
`cocco` where the package is installed, and it is the one of the three commands that needs nothing
but a URL and a token to be useful. `COCCO_URL` and `COCCO_TOKEN` are
its configuration, and that is a *client's* — nothing to do with `cocco.toml`, which is a
deployment's. `pull` extracts into a directory by default, because a directory is the thing you can
edit; `push` zips **only** `challenge.toml`, `README.md`, `tests/` and `evaluator/`, so a `.git` or
an editor's swap file is never sent, and it checks every member's path on the way out of a zip even
though that zip came from our own server.

### Exercises (`cocco/exercises/`, the `exercise_*` tables)

Short Python exercises, wooclap-style: a teacher runs a set live in class, sets the pace, and
watches who has solved what. The feature is a port of the `esercizi-python` prototype, with the
judge in place of Pyodide (DESIGN.md says why). It is **purely formative**: nothing is graded,
exported or ranked, and "solved" is a verdict the server reached. **Exercises are not challenges.**
They share `Language`, `isolate_run`, the box pool and the prewarmed runtimes, and nothing above
that. Folding them into `challenge` would put a "but not for exercises" branch into every
standings, leaderboard, transfer and regrade path. `plans/2026-10-17-interactive-exercises.md` is
the long form of everything below, including the measurements.

**The `.md` file is the source of truth, and every import of it is an immutable version.** There
is no `exercise` table and no `exercise_test` table. `exercise_set_version` holds the source
verbatim (solution included, staff-only) and the **compiled document** derived from it: the parse
plus the expected outputs the judge computes, as one JSONB value whose shape is
`schema.ExerciseDocument` (with a `format` number). The source is written at insert. The document,
the problems and the status are written **once**, by the build that finishes the version
(`building` → `ready` | `failed`), in one `UPDATE` guarded by `status = 'building'`. Two values
written once and never updated cannot drift, and that replaces a rule anybody would have to
remember. Three pointers hang off it:

- `exercise_set.current_version_id` is the newest **ready** version. `finish_version_build` moves
  it in the build's own transaction, and **only forward**, by id, because two quick imports are two
  builds that may finish in either order. A failed re-import therefore breaks nothing.
- A session **pins** `version_id` when it is opened.
- A job records the version it ran against, so a result is always read against the tests it was
  graded on.

An exercise has no id: it is its **1-based position** in a version, the `{n}` in every URL.
`exercise_set` and `exercise_set_version` point at each other, so the set's foreign key is
`use_alter`, and a delete clears `current_version_id` before it can take the versions.

**The format** is `cocco/exercises/format.py`, a port of the prototype's `formato.js` with
**English keywords and one spelling each**. The front matter has `title:` / `description:`, each
`# Heading` starts an exercise, and the fenced blocks are ` ```starter `, ` ```solution `,
` ```input [hidden] `, ` ```output `, ` ```error ` and ` ```hint `, with `compare:` and `time:`
lines. Its module docstring is the reference. The prototype's Italian keywords are **errors that
name the English word, not aliases** (`_ITALIAN`). A ` ```soluzione ` block left unconverted would
otherwise be "any other code block", which stays part of the statement and would put the solution
on every student's screen. Parsing is synchronous, at import: a file that does not parse is a 200
with its problems and creates no version.
`compare.py` holds the comparison rules (`exact`, `spaces`, `case`, `numbers <tol>`, `order`).
`hints.py` holds the one-line English explanation of each Python exception. Like `transfer.py`,
`format.py`, `compare.py` and `hints.py` touch no database, FastAPI or judge, and are tested by
import.

**Nothing runs the student's file directly: `harness.py` does, inside the box.** The judge copies
it in next to the program and runs `python harness.py <check|run> main.py`. It is not imported by
cocco, so it stays plain stdlib. It exists because two semantics are about Python itself:

- an `input("…")` prompt is dropped in `check` mode, and in `run` mode it is echoed together with
  the line read;
- `error: ValueError` passes on that exception or a subclass, which needs the MRO, and only the
  raising process has it.

It caps stdout, stderr and the traceback at 256 K characters each, always exits 0, and writes one
JSON object, `schema.HarnessReport`, as the **last line** of the real stdout. **The isolate status
says how the run ended; the report says what the program did.** A timeout or an OOM produces no
report. A missing or unparseable report under status `OK` is something the *program* did
(`os._exit`, closing fd 1), reported as "ended without reporting", and never a
`judge_internal_error`. A program can forge the report by writing to fd 1, and that is accepted:
the output is still compared in the judge, so a forgery can only fake the exception check, and
nothing is graded. `_jsonb_safe` strips NULs and lone surrogates **as the report is parsed**, not
on the way into the database. A solution's output becomes an expected output, so one boundary
keeps what is compared and what is stored the same bytes.

**Comparison happens in the judge process**, not in an evaluator box: the rules are declarative
and the code applying them is ours, and a box per test would double a check's cost to buy nothing.
It goes through `asyncio.to_thread`, because `order` with `numbers` is quadratic in the line count.

**Three job kinds, one table, `exercise_job`, with the result as JSONB on the row.**

- **build**: staff, one per version. It runs the solution twice on every input (to catch
  non-determinism) and the starter once, then writes the document and the problems. Errors fail
  the build: a crash, a timeout, or an exception where no `error` was declared. Warnings do not: a
  starter that already passes, a non-deterministic solution, every test hidden.
- **check**: a recorded attempt. Every test runs through the harness, concurrently, and the
  result holds a verdict per test plus `passed`.
- **run**: the student's own stdin, given up front (Run is not interactive), with nothing
  compared. The transcript reads like a terminal because the harness echoes what was read.

Because there are no child rows, a judge finishing a job whose session, set or course has just
been deleted updates **zero rows** (`finish_exercise_job`) instead of violating a foreign key. That
is why no delete here has to lock against the judge, unlike `lock_challenge_submissions`. **One
queued job per button**: `insert_exercise_job` is an upsert on `uq_exercise_job_one_queued`,
`(user_id, kind, session_id, version_id, exercise_index) WHERE status = 'queued' AND kind <>
'build'`, `NULLS NOT DISTINCT` so that previews dedupe too. A second click *replaces* the queued
job's source and stdin and keeps its id and its `enqueued`, so mashing a button builds no backlog,
loses no place in the queue, and leaves a polling tab looking at the right row. A `running` job is
outside the predicate and is never touched. That index is also why `requeue_exercise_job` tries
inside a savepoint and finishes the job as superseded when a newer one holds its place.
**Retention**: `prune_run_jobs` deletes finished runs and session-less (preview) checks older than
`RUN_JOB_RETENTION`, six months like the logs. It runs from an idle worker at most hourly, in
batches. A session's checks and every build are kept, because progress and problems are derived
from them.

**A preview is a job without a session.** Staff try a version at `…/exercises/{set_id}/versions/
{version_id}/preview/{n}` as a student would. `session_id` is NULL, so no progress query ever sees
it. Course staff are **refused** Run and Check *inside* a session (`STAFF_SESSION_JOB_REFUSAL`,
with a link to the preview), so a session's jobs are only ever its students' work. Staff may still
open its pages past the pacing.

**Sessions carry pacing and progress; a set is only content.** `exercise_session` has a label, a
`mode` (`live` or `open`), `unlocked_through`, `opened_at` and `closed_at`, and one set can run
many sessions: lab group A on Monday, group B on Wednesday. In **live** mode a student may read and
attempt `1..unlocked_through` and gets a "wait for the teacher" page past it, whose poll answers
`HX-Redirect` once the exercise unlocks. **Open** has nothing locked and is the homework mode.
**Closed** refuses every new job and still lets everyone read. Every session write is one
conditional statement, `block_user`'s style. **All of pacing is `exercise_lock_refusal`**, called by
the read gate (`require_session_exercise`, behind `require_exercise_session_member` and its
`exercise_session_in_course` cross-check) and by the job gate (`_session_job_gate`). The job gate
reads the session row `FOR SHARE` inside the transaction that inserts the job, so a close, a pace
move or a version switch (all `UPDATE`s of that row) waits for the job to commit. A locked exercise
is a `Forbidden` page, not a 404, for the scheduled challenge's reason.
`exercise_session_participant` is written on a student's first visit and is the dashboard's
denominator: who came, not who is enrolled. `exercise_session_reveal` shows one exercise's
solution to the class. Code does not carry over between sessions. The draft lives in localStorage
per session and exercise, and every job sends a copy to the server, which the page falls back on.

**Switching a session's version is the one write that changes `exercise_session.version_id`**, and
re-importing never does it by itself: a fixed file builds a new version, it becomes current, and
staff press "switch to the latest version" (`post_exercise_session_switch`).

- Exercises are matched **by position**. The switch therefore **refuses** a version with fewer
  exercises than the session has checks for (`query_max_checked_exercise`, read under the row's
  `FOR UPDATE`), and its confirmation lists every retitled exercise, since a reordered file is the
  one mistake position-matching cannot see.
- It is refused unless `version_id` is still the current version the confirmation showed.
- `unlocked_through` is clamped to the new count, or set to it for an open session, and reveals
  past the end are dropped.
- Past checks keep their version and their result, so "solved" stays solved. Nothing is regraded.

**Progress is derived, never stored.** On each exercise a student is *solved* (some check passed
every test; this is **sticky**, surviving later wrong attempts and version switches), *attempted*,
or *not attempted*, with *checking* shown beside the status while a check is pending.
`_exercise_status` is the one function that decides it, for the student's strip and the teacher's
grid alike. Both read the check aggregate `_session_checks_cte(one_user=…)`, so the two screens
cannot disagree. The cohort is `_SESSION_COHORT_CTE`: participants, unioned with anyone who
checked. **Two staff screens, deliberately separate pages.** The **dashboard** (`…/dashboard`,
`query_session_progress`) shows names, a participants × exercises grid, and a drill-in to a
student's latest Check and Run. The **projector** (`…/projector`, `query_session_counts`) never
selects a person, only counts. It has the pacing and reveal controls, and the forms post
`view=projector` so they answer with the projector's own fragment. A projected screen must never
be able to show who is stuck, and that is a property of the query, not of the template.

**The student never receives the compiled document.** `student_exercise_view` is the one function
that cuts it down: statement, starter, hints, visible tests with their expected outputs, a count of
hidden ones, and the solution only once revealed. It is `visible_outcomes`'s rule, kept in Python
where it is testable. `exercise_check_rows` does the same for a check's result. A hidden test
shows a student only its verdict and, on an error, the exception's type, which its hint explains.
The line is withheld too: it is a channel the program controls, as are its output, its exit code
and its running time. The page
polls only the strip, every 3 s, and **never swaps the editor**. A job's fragment polls every 500
ms while pending. **The poll ends because the done copy replaces the polling element and carries no
trigger, not through a 286**: the htmx 4 this app loads has no special case for 286
(`_render_exercise_job`). The editor is CodeMirror 5, vendored, because 6 needs a bundler.

**Routes and authorization are course-keyed.** Sets live under `/courses/{course_id}/exercises/
{set_id}/…` and sessions under `/courses/{course_id}/sessions/{session_id}/…`. The role always
comes from `course_id` through `CourseEditor` / `CourseMember`, with an `*_in_course` cross-check
(`exercise_set_in_course`, `exercise_session_in_course`), never by joining through the set. The
per-exercise route is **`…/sessions/{session_id}/{n:int}`, and the convertor is load-bearing**: a
plain `{n}` also matches `…/dashboard` and `…/projector` and answers 422 on them, because a route
does not fall through to the next one on a failed validation. Everything is declared above
`get_challenge`, with the course's other pages. **An archived course refuses every Run and Check**,
in a session and in a preview, with `ARCHIVED_EXERCISE_JOB_REFUSAL`. That is archiving's third
enforcement point. A blocked student is stopped by `get_user`, and their queued jobs still run,
for `pull_queued_submission`'s reason.

**Deletion is hand-ordered, like everything else here, with no `ON DELETE`.**
`delete_exercise_session` deletes jobs, reveals, participants, then the session; it is allowed in
any state, and staff confirm by typing the label. `delete_exercise_set` refuses a set that still
has sessions. Otherwise it deletes the jobs **by `version_id`** (builds and previews have no
session and would hold the versions down), clears `current_version_id`, then deletes the versions
and the set. `delete_course` calls both, after `lock_course_exercises` has taken sessions, then
versions, then sets `FOR UPDATE`, which is the order every writer takes them in.

**Transfer**: `cocco pull --set ID` / `cocco push` move a set as one `exercises.md`
(`transfer.EXERCISES_NAME`, `cli.Kind`), under the existing `.cocco/state.toml` discipline. The
`ETag` is the **newest** version's id, not the current one's. Pinning it to the current version
would make every push after a failed build a 412, and would hand a pull the old file, which
`reconcile` would then write over the author's fix. A push is an import, holds
`lock_exercise_set_for_push` from the `If-Match` check to the commit, and waits for the build
unless given `--no-wait`.

### Logging (`cocco/logs.py`)

**Call sites do not change; only the output does.** Every module logs through
`logging.getLogger(__name__)` with `%`-style messages, and `[log] format = "json"` turns each record
into one JSON object on stdout (`JsonFormatter`); `"text"`, the default, is what a developer reads. A
value worth filtering on is *also* passed through `extra=` — the judge's per-test `outcome`,
`elapsed_s` and the final `score`; the sign-in refusals' `email`, `provider` and `reason`; the
config warnings' `setting`. The module is `logs`, never `logging`, which would shadow the stdlib.
Output is stdout and nothing else (stderr for `cocco-migrate`, whose `--sql` owns stdout); retention
is the collector's job, six months, and `cocco.example.toml` says how and why.

**Request-scoped fields are `bind`, a `ContextVar`, copied onto each record by `ContextFilter`** —
on the *handler*, because a logger's filters do not run for records propagated from its children.
`AccessLogMiddleware` binds `request_id` for a request and `run_from_queue` binds `submission_id`,
`challenge_id` and `language` for a graded run, so every line underneath carries them without being
edited. A test reading those off `caplog` has to add a `ContextFilter` to `caplog.handler`.

**Logging reaches uvicorn as `log_config=`, a `dictConfig` dict, never as handlers set up in
`cocco-web`'s process.** Under `--reload` and `--workers` the app runs in a child that inherits none
of the parent's handlers but does apply `log_config` — and the same dict takes over `uvicorn.error`,
so there is one format in the stream. `log_level` is passed as None because it would override the
dict's levels, and `access_log=False` because `cocco.access` replaces uvicorn's line. Each process
configures logging from `load_log_config` — the `[log]` table alone — *before* `load_config`, so the
latter's warnings come out in the format the deployment asked for; a `[log]` table that cannot be
read is the one refusal that is a bare `print`, having no log to go to. `--log-level` on any of the
three commands overrides the file for one run.

**One `cocco.access` event per request**, from `AccessLogMiddleware`, a pure ASGI middleware
registered *last* in `main.py` so it is outermost (`add_middleware` puts each new one outside the
others) — pure ASGI rather than `BaseHTTPMiddleware`, which would run the app in another task and lose
the bound context. It carries the route **template** (FastAPI's `scope["route"]`, a mount's prefix +
`/{path}`, None for a 404), `status`, `duration_ms`, `client_ip`, `user_id` from the
`request.state.user` that `get_user` stashes, `hx` and a truncated `user_agent` — and **never the
query string**, because the OAuth callback's carries `code` and `state`. A 2xx/304 on a `/f/...`
route is DEBUG and everything else INFO, so a failing poll still shows; an exception escaping the app
is ERROR with its traceback, then re-raised. The id goes back as `X-Request-ID`.

**`[site] proxy_headers` is handled inside the app, not by uvicorn**, and the order is the point:
`AccessLogMiddleware` wraps uvicorn's `ProxyHeadersMiddleware` *inside* itself, because an incoming
`X-Request-ID` is adopted only from a peer in `[site] trusted_proxies` (and only if it matches
`[A-Za-z0-9-]{1,64}`) — otherwise a client could choose the id another request is filed under — and
the proxy's own address is only visible before `ProxyHeadersMiddleware` rewrites `scope["client"]`.
It also keeps the trusted list in the file rather than in uvicorn's `FORWARDED_ALLOW_IPS`, which is
the environment. `config.py` validates every entry as an address or a CIDR network, because uvicorn
keeps anything unparseable as a literal that matches nothing — and the symptom of that is every
request logged with nginx's address. `cocco-web --proxy-headers` survives one release, warned about:
it still makes uvicorn rewrite the client and scheme (from the same trusted list), but only the file's
spelling adopts nginx's request id.

## Conventions specific to this repo

- **Async everywhere in `judge.py`.** Subprocess calls use `asyncio.create_subprocess_exec`; tests are `async def` and rely on `asyncio_mode = "auto"` in `pyproject.toml`, so no `@pytest.mark.asyncio` is needed.
- **Box ids are a shared resource.** Always go through `_IDS_POOL.acquire()` rather than hardcoding a box id; the pool defaults to 32 concurrent boxes.
- **Large inputs live on disk.** Test case inputs/outputs are passed by path, not read into memory, and reach a box a chunk at a time. Preserve this when adding features — uploaded test files go through `Storage.save_stream`, never `await upload.read()`, and a file that leaves the app (the sample download, the export zip) is streamed rather than handed over whole. **Exercises are the deliberate exception, and nobody should "fix" it.** Their inputs are a few bytes an author typed into a Markdown file, so they live in the database, in a version's compiled document. The judge reads an exercise's tests in one query (`query_job_exercise`), and `Storage` plays no part in that feature at all.
- **All file access goes through `Storage`.** `Storage.resolve` refuses any path that would land outside the storage root, and `safe_filename` reduces a user-supplied name to a bare, harmless one. Test case paths come from the database and ultimately from teacher input, so never join them onto a base directory by hand.
- **Everything under the storage root is a gzip stream stored under its logical name**, and `cocco/storage.py` is the only module that knows it. A million-line input is the shape that compresses best and a course keeps hundreds of them; `write`, `read`, `open` and `save_stream` take and return plaintext and are the calls they always were, so the compression is *transparent* and `main.py`'s call sites mostly did not move. There is deliberately **no `.gz` suffix**: `testcase.input_path` is still the name the teacher uploaded and the name the transfer format uses, which is what leaves `resolve`, `exists`, `rename`, `delete`, `plan_testcases` and the rename route untouched — a suffix would have to be appended by every writer and stripped by every reader, and one that forgot would be silent. Three consequences. `save_stream` returns the **uncompressed** count, because that is what `MAX_TESTCASE_BYTES` is about, while `size` is bytes on disk. Deflating tens of megabytes is a second of CPU, so an async caller puts it on a thread — `post_testcase`, the push's `store`, the export's zip build — which is the rule `hash_password` is already here for. And a file leaving the app is a decompressing **stream**, never a `FileResponse`: `_stored_file_download` for the two downloads, `_zip_stored` for the export, since there is no file of the right bytes to point one at. There is no fallback for a plaintext root and deliberately so: `cocco-migrate compress-storage` converts one, `check_storage_format` refuses to start without the `.storage-format` marker (`check_schema_is_current`'s shape, in `cocco/server.py`, the startup hook and `judge.main`), and a read that meets a plain file raises `StorageFormatError` naming that command rather than letting `gzip` notice halfway through grading. The bundled evaluators in `cocco/evaluators/` are **source tree, not storage**, so they are plain — the one branch `prepare_evaluator` and `get_evaluator_download` each keep.
- **`challenge.position` orders, `number` numbers.** The column is the sort key the course editor's arrows write, ordered by `position, id` everywhere so challenges nobody has reordered keep the order they were created in. What a page prints ("Challenge 03") is `number`, the *rank* of that key within the course, computed in `query_challenge` — the two are separate fields because a reorder leaves gaps and skipped numbers would be a bug. Moving one is `reorder_challenges`, a dense renumber of the whole course rather than the sentinel swap `swap_testcase_order` does: `position` defaults to 0, so every row of a course that predates the column starts out tied, and exchanging two zeroes moves nothing.
- **A test case that has graded something is never deleted, only retired.** `delete_testcase` refuses (via `count_testcase_outcomes` in the route, with the `testoutcome.testcase_id` FK as the backstop) once any outcome points at it: detaching those outcomes would leave scores in the history with nothing to say what produced them. Clearing `is_active` is the supported move — the row, the files and the past outcomes all stay, but the judge stops running it and it stops being downloadable. `is_active` is checked in `pull_queued_submission`, in `query_challenge`'s `samples` CTE and in `query_sample_testcase`; the editor's `query_challenge_testcases` deliberately does not filter it, so a retired case can be reinstated.
- **Regrading is the one place outcomes are deliberately destroyed.** `regrade_challenge` deletes every outcome of a challenge and puts every submission back to `queued`, so a fixed expected output can be applied without asking students to resubmit. Outcomes scored on *retired* test cases go too and never come back, since the judge only runs active ones — that is the difference between regrading and retiring, and it is why the confirmation dialog spells it out. Afterwards `count_testcase_outcomes` is zero for the challenge's test cases, so they are deletable again; nothing is left for a delete to strand. The route checks `count_running_submissions` first and refuses while a judge is mid-run, whose outcomes would otherwise land after the reset.
- **Deleting a challenge is the one place a submission is destroyed, and the cascade is hand-written.** No foreign key in `model.py` declares an `ON DELETE`, deliberately — `testoutcome.testcase_id` having none is exactly what makes it the backstop that stops `delete_testcase` from stranding a score — so `delete_challenge` spells the order out once (testoutcome by `submission_id`, then by `testcase_id`, then submission, testcase, challenge) and the database's job is to raise if it is ever got wrong. Both outcome deletes and not one: both FK columns are nullable, so an outcome detached from its submission can still be the only thing holding a test case down. `delete_course` calls it per challenge rather than writing the cascade a second time, wider. It does the same with `delete_exercise_session` and `delete_exercise_set` for the course's exercises (see "Exercises"), and then takes enrollments and the course. Every other rule in this app draws the opposite line — a block does not retract work, `delete_enrollment` takes one row, a graded test case is retired — and that line is about a course that still exists; there is nothing left for a score to be the record *of* once the challenge is gone. Files are read **before** the deletes (`query_challenge_file_paths`) and unlinked **after** the commit, `post_testcase_delete`'s order, so a failed commit can leave a file nothing points at but never a row pointing at a file that is gone; the directories then go through `Storage.delete_directory`, which is `rmdir` and never `rmtree`, so anything still in there is left to be looked at.
- **A delete locks the submissions it is about to destroy; a regrade only counts them.** `post_regrade` is happy with `count_running_submissions` because the race costs one stale score. A delete uses `lock_challenge_submissions` / `lock_course_submissions`, which take the rows `FOR UPDATE` and count in the same statement, because `run_from_queue` writes its outcomes inside `engine.begin()` with **no `except` around it**: a submission deleted mid-run makes `insert_test_outcomes` violate a foreign key, which fails that worker's job with a traceback in the log (it used to take the judge *process* down). The locks are taken on `submission` before anything touches `testoutcome`, which is the order the judge takes them in, so the two cannot deadlock.
- **"Archived" is one `or`, spelled twice and never a third time.** A challenge is archived when it says so *or* when its course does; `_EFFECTIVE_ARCHIVED_SQL` says it for the raw page queries and `_effective_archived_at()` for the single-row reads a route makes, both in `repository.py`, each naming the other, with a test asserting they agree. Every page query exposes it as **`effective_archived_at`** — a separate name and never `AS archived_at`, which would put two columns of that name in the record and let `row_to_json` decide it last-one-wins — and every template reads that one. The single exception is the challenge editor's danger zone, which compares `challenge.archived_at` against it to say "its course is archived, so this is read-only whatever this button says"; anywhere else, reading the challenge's own column silently un-archives every challenge under an archived course. `query_courses` drops an archived course from the **non-enrolled half only**, since it cannot be joined and listing it would be an invitation to a shut door; `include_archived` is how an administrator, the only person who can unarchive one, still finds it. `pull_queued_submission` deliberately does not learn about any of this: skipping an archived challenge's queued rows would strand them as `queued` forever, exactly as it would for a blocked student's.
- **"Visible" is `challenge.visible_from`, and it is `archived_at` inverted.** NULL is hidden, a past instant is visible, a future one is a challenge that **opens by itself** at that moment — three answers to one question, so one column. It keeps its neighbour's discipline and is spelled exactly twice, `_CHALLENGE_VISIBLE_SQL` for the raw page queries and `_challenge_visible()` for the single-row reads, each naming the other, with a test pinning them together. Two differences from archiving are load-bearing. It does **not cascade from the course**, which has no hidden state to cascade, so the Core expression needs only `challenge` in its FROM. And it is enforced in *one* place rather than at the routes: `require_challenge_visible` is called from `require_challenge_viewer` and `require_challenge_member`, the two gates that already answer "may this person read this challenge" — which is why `post_submission` has no visibility check of its own and needs none, its dependency running before its body. The two refusals differ on purpose: a **hidden** challenge is a bare 404, which tells the asker nothing and matches the course page they came from, while a **scheduled** one is a `Forbidden` naming the date, because its teaser card is on that page saying exactly that and a 404 there would read as a broken link. `insert_challenge` writes NULL, so **a new challenge is hidden** — it has no test cases and no evaluator, and listing it the instant it was created was advertising a page that could not be solved. `_COURSE_CHALLENGES_CTE` exposes it as **`is_visible`** (never `AS visible`, `effective_archived_at`'s reason) and its two callers take `include_hidden`, defaulting to the *student's* list so a caller who forgets cannot leak one. Unlike `archived`, it **is** a `challenge.toml` key, because it is a column a challenge form writes: absent means hidden, so a pulled draft pushed back stays a draft. `pull_queued_submission` does not learn about it either, for archiving's reason.
- **The number on a challenge card comes off the row, never off `loop.index`.** `_COURSE_CHALLENGES_CTE` carries `number`, a `row_number()` over `position, id` computed inside the CTE *before* `include_hidden` filters anything, which is the same rank `query_challenge` computes and `_statement.jinja` prints. Counting rows in the template was fine while every viewer saw the same list; the moment a student's is filtered, it would print "Challenge 03" to them in front of what staff call 04.
- **Language runtimes are pinned by `flake.lock`.** Changing a compiler version means updating the flake, not the system.
- **Storage is compressed, an isolate box is plain**, and `isolate_run` is where the two meet. Its `stdin` is `None` for an empty standard input or a **compressed file in storage** — one rule with no flag to get wrong, and what replaced the old `_DEV_NULL` path — and `_feed_stdin` inflates it into a pipe as the box runs, so a million-line input is still never written out whole. It is `communicate()` written out by hand for exactly this: given a stdin pipe, that method closes it whether or not it was given anything to write, so the box would see EOF mid-inflate. A broken pipe there is *ordinary* — it is what a program that stops reading early, or times out, looks like from this end — and the box's own status is what says how the run ended. `compressed_box_files` is the same crossing for a file the box opens by name: it expands rather than copies, which keeps the evaluator's input at the one write `shutil.copy` was already doing. Both go through `asyncio.to_thread`, because zlib releases the GIL and a judge runs boxes concurrently.
- **An isolate box is one flat directory, and nothing reaches it by guesswork.** `isolate_run` passes `args` exactly as written and copies a file in only when it is named in `box_files`, `compressed_box_files` or `additional_files` (the last under its basename); an argument naming a file names it box-relative, which is why `Language.run` formats `{executable}` as the basename and `{workdir}` as `.`. It used to copy in any argument that happened to name an existing path, resolved against the judge's working directory — `/` under systemd — so a java `public class run`, whose name java gets as an argument, copied `/run` and its credentials into a box the student could read. The copy order — `box_files` and `compressed_box_files`, then `additional_files` — decides who wins a collision. `box_files` is the explicit form and the only safe one when more than one file is involved: the evaluator gets its three arguments as `.actual_output`, `.expected_output` and `.test_input`, names nothing can collide with because `safe_filename` strips leading dots from every upload. Without it, a checker uploaded as `expected_output.c` compiles to a binary named `expected_output` that lands on top of the expected output it was meant to read.
- **A submission is compiled under the name it was uploaded with, and a compiled + VM language ships every artifact.** Two rules that only look like housekeeping until java arrives. `run_submission` writes the source as `source_filename(run_data.filename, runtime)` — the student's name, sanitised through `safe_filename` *there* rather than at upload, since `post_submission` only takes a basename and rows already in the database carry whatever that let through — with the *language's* canonical extension rather than the uploaded one, because the extension is what tells a compiler what it has been handed and `Main.JAVA` is a java submission javac would refuse to read. It used to be a flat `source.<ext>`, which made java the one language nobody could submit: javac requires `Foo.java` for `public class Foo`, and `Language.compile` then looks for `Foo.class` because it derives the artifact from the stem. `prepare_evaluator` had it right all along, which is why a java *evaluator* worked while a java *submission* could not. Behind that sits `_run_artifacts`: javac writes one `.class` per class, not per file, so a box holding only the entry point dies with NoClassDefFoundError on the first `Main$Helper`. It globs the compile workdir, which is sound only because a submission's classes land in the run's temp dir and an evaluator's in `evaluator/` beneath it, and it travels as `additional_files` rather than `box_files` because `evaluate` owns that argument for the checker's three inputs. `filename` is therefore on `SubmissionRunData` with no default, so a query that drops the column fails loudly instead of quietly grading everything as `source` again.
- **Scores are per test case, in `[0, 1]`.** A submission's headline score is the mean of its outcomes' scores, shown out of 100. This aggregation assumes higher-is-better, so it is wrong for a challenge whose evaluator uses another scale — nothing enforces the range, and an evaluator is free to return any finite float.
- **The leaderboard's criterion is per challenge.** `challenge.ranking` is set by course staff in the editor and drives `query_challenge_leaderboard`, whose three variants live in `_LEADERBOARD_CRITERIA` as literal SQL keyed by the enum — the criterion is never taken from a request as text. `running_time` is the strict board: a submission is on it only if it passed *every* test, ranked by the sum of the per-test times. `total_score` and `average_score` rank by score, descending, and deliberately admit partially correct submissions, because a partial-credit evaluator is the case where nobody passes everything; both assume higher-is-better and so are wrong for an evaluator that is not, same as the headline score. Whichever the criterion, a student appears once, represented by their best submission, and every row carries all three metrics plus `n_tests` so the fragment can render the one that applies.
- **A block stops a person, it does not retract their work.** The judge grades a blocked student's queued submissions — `pull_queued_submission` does not know who submitted and should not learn: skipping those rows would strand them as `queued` forever, showing as pending in every teacher's standings and muddying the `count_running_submissions` check that guards a regrade. Standings and leaderboards go on listing them for the same reason: they are the record of what happened on a course, and it did happen. Nothing blocked is seeded either — a blocked seeded administrator would lock a developer out of their own fresh checkout the moment they typed him into the sign-in box, which is exactly the trap "an administrator is never blocked" exists to avoid. **Removing an enrollment draws the same line**: `delete_enrollment` takes one row and nothing else, so the person loses access to the course and drops out of its standings, while every submission, outcome and leaderboard entry stays exactly where it was — `submission.user_id` has no `ON DELETE` tie to `enrollment`, and `query_challenge_standings`'s cohort is already a union with everyone who submitted. The confirmation on the people page says so in as many words.
- **A hidden test case gives away nothing but its verdict.** `visible_outcomes` in `main.py` blanks an outcome's `message`, `stdout` and `stderr` unless the test case is a public sample or the viewer is course staff: `stdout`/`stderr` are the submitted program's own output — the obvious way to print a hidden input back out — and `message` is now written by a *teacher's script that has just read the hidden input and the expected output*, which makes the rule matter more, not less — `diff.py` puts the expected output in it verbatim. The verdict, the score and the resource usage are always shown. Compiler output is exempt, and has its own block on the submission page: it does not depend on any test input. Redact in that function, not in the template, so the rule stays unit testable.
- **An anonymous htmx request gets `HX-Redirect`, not a 302.** `get_user` raises `NotAuthenticated` rather than returning a redirect — a dependency cannot return a `Response` — and the handler branches on the `HX-Request` header. This matters because most of the app is `f-*` fragments polled every few seconds: htmx follows a redirect inside the XHR, so a 302 would splice the whole login page into whatever div was polling, over and over. A 204 with `HX-Redirect` navigates the browser instead.
- **A secret has one source, and never a fixed default.** The source is the environment — `COCCO_SECRET_KEY` and each provider's `COCCO_{GOOGLE,GITHUB}_CLIENT_ID`/`_SECRET` — and there is no file spelling for any of them, so there is no precedence to get wrong and nothing secret in a file that gets copied, diffed and committed by mistake. It is also what makes `cocco.dev.toml` committable at all. With `COCCO_SECRET_KEY` unset, `load_config` generates a random key per process and logs a warning: an unconfigured deployment then logs everybody out on every restart and no two workers agree, which is loud. A constant in the source tree would be a signing key that ships with the code, which is silent.
- **A `next` destination is filtered, never trusted.** `safe_next_path` keeps only a path on this site — one leading `/`, no scheme, no authority — so neither the `?next=` on the login page nor the one stashed in the session can be turned into an open redirect. It is applied at both ends: before the destination is stashed, and again after it is read back. The OAuth redirect URI deliberately carries no destination at all — a provider matches that URI character for character against what was registered with it, so `next` travels in the session instead, and a callback arriving without one lands on the root.
- **CSRF rests on `same_site="lax"`, app-wide.** `POST /logout` is a form rather than a link so a planted `<img>` cannot fire it, and `lax` means the session cookie is not sent on a cross-site POST at all. That is the whole defence, and it is the same one every other mutating route in the app relies on — there is no CSRF token anywhere. A deliberate position, not an oversight: changing `same_site` would silently remove it.
- **Source is highlighted server-side.** `render_source` (the `highlight` Jinja filter) runs Pygments over a submission's source, using `submission.language` — every name in `languages.toml` happens to be a Pygments alias too — and falls back to plain text otherwise. The token colours are hand-written under `.cocco-code` in `assets/styles.css`, not a generated Pygments theme.
- **CSS is a build artifact.** Edit `assets/styles.css`, never `cocco/static/styles.css`. Tailwind only emits classes it can see, so it scans `cocco/templates/**/*.jinja` and `cocco/**/*.py` (`@source` directives); a class assembled dynamically at runtime will not be generated. The input stays in `assets/` and the output lands in the package, which is the same line the packaging draws everywhere else: `assets/` is what the build reads, `cocco/` is what ships. The two multi-megabyte icon sources live there too, and only files a template actually names are under `cocco/static/`: the three every page loads (`icon-small.png`, `favicon.ico`, `styles.css`), and the exercise editor's - `exercise.js` and CodeMirror 5 under `codemirror/`, vendored from its npm release with its MIT `LICENSE` beside it and kept out of tailwind's scan (`@source not`).
- **A static URL carries its own version, and that is what decides its `Cache-Control`.** `static_url()` stamps the file's mtime as `?v=`, and `VersionedStaticFiles` answers a URL that has one with a year of `immutable` and one that has not with `no-cache` — so a versioned URL can never serve a stale file (it changes when the file does) and a URL typed by hand can never be over-cached. The two halves only make sense together: bare `StaticFiles` sets no freshness directive at all, so every reload revalidated the navbar mark and the navbar painted an empty box until the 304 came back. That was the flicker. The mtime is read **per render, not at import**, because `npm run watch:css` rewrites `cocco/static/styles.css` without touching a `.py` and there would otherwise be nothing left to invalidate a year-long `max-age`; a missing file falls back to the bare path, since the stylesheet does not exist until the first `npm run build:css`. `STATIC_DIR` and the `/static` mount now name **one** directory: they used to be two — the version stamped on a URL came from the checkout while the bytes were served from `$CWD/static` — which was a stale-file bug waiting for someone to start the app from somewhere else. The mark is additionally **preloaded above the stylesheet** — the preload scanner promotes it instead of queueing it behind render-blocking CSS — and `base.jinja` binds `logo_url` once for both, because a preload whose href differs from the `src` by a single byte fetches the image twice.
