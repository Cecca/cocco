# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`cocco` is a self-hosted judge for programming challenges. Submissions are executed inside the [isolate](https://github.com/ioi/isolate) sandbox (the IOI/ICPC judge), using language runtimes supplied by a Nix flake. The HTTP layer is FastAPI with Jinja templates and htmx; persistence is the SQLAlchemy 2.0 Core API on PostgreSQL.

`DESIGN.md` has the rationale for the isolate + Nix choice and the alternatives that were rejected; read it before proposing architectural changes.

## Commands

Dependencies are managed with `uv` (Python 3.13). A `.envrc` / Nix `devShell` auto-creates `.venv` and activates it.

- Install deps: `uv sync`
- Start / stop PostgreSQL: `pg-start`, `pg-stop` (then `create-db cocco-dev` once)
- Run the API (dev): `COCCO_DUMMY_AUTH=1 uv run fastapi dev cocco/main.py`
- Reset and reseed the database: `COCCO_RESET_DB=1 uv run fastapi dev cocco/main.py`
- Configure Google sign-in and the session secret: `cp cocco.example.toml cocco.toml` and edit it
- Run the judge: `uv run python -m cocco.judge --database "postgresql+psycopg://$PGUSER@/$PGDATABASE?host=$PGHOST"`
- Run all tests: `uv run pytest`
- Run only unit tests (skip those requiring isolate + nix + postgres): `uv run pytest -m "not integration"`
- Run a single test: `uv run pytest cocco/test_judge.py::test_language_python`
- Build a language runtime on its own: `nix build .#python3` (or `.#gcc`, `.#jdk`)
- Build the CSS: `npm install` once, then `npm run build:css` (or `npm run watch:css`)

Integration tests (`@pytest.mark.integration`) require `isolate` installed with its setuid bit, a working `nix` with flakes, the current user being allowed to run isolate boxes, and — for the repository tests — a running PostgreSQL. On NixOS, isolate is declared via `security.wrappers`; on other distros install it outside Nix. Note that the repository integration test **drops every table** in `$PGDATABASE`.

The web app and the judge are **separate processes** that must agree on two things: the database, and the storage root for test case files (`COCCO_STORAGE`, or the judge's `--storage`; both default to `/tmp/cocco-storage`). Submitting through the web UI only enqueues a row — nothing leaves `queued` unless a judge is running against the same database.

`COCCO_RESET_DB=1` drops every table and reseeds dummy data on startup. Leave it unset in normal development: `fastapi dev` reloads on every file change, and each reload would wipe the data. There is no migration tool, so a schema change reaches an existing database only through one such reset.

Runtime configuration lives in `cocco.toml` (path overridable with `COCCO_CONFIG`), read by `load_config` in the same shape `load_languages` reads `languages.toml`. It is gitignored — it holds the Google client secret and the session signing key — and `cocco.example.toml` is the commented template. **A missing file is not an error**: with no `[auth]` credentials the app boots and the login page says Google sign-in is unconfigured. `COCCO_DUMMY_AUTH=1` is what makes a fresh checkout usable at all — it needs no credentials and no click, and `just dev` sets it by default.

## Architecture

### Execution pipeline (`cocco/judge.py`)

Three layers, loosely coupled:

1. **`resolve_runtime(runtime_name, bin_name)`** — shells out to `nix build .#<runtime>` and returns the `/nix/store` path to the requested binary. Runtimes are whatever the flake exposes (`python3`, `gcc`, `jdk`). The full store is later bind-mounted into the sandbox via `--dir=/nix/store` so the interpreter/compiler can find its libraries.

2. **`isolate_run(exe, stdin, args, ...)`** — the sandbox primitive. Acquires a box id from `_IDS_POOL` (a bounded `asyncio.Queue` of ids, so concurrent runs don't collide on isolate box numbers), runs `isolate --init`, copies any file/dir arguments into the box, runs `isolate --run --cg` with cgroup-based time and memory limits, parses the `--meta` file, then `isolate --cleanup`. Returns a `RunOutcome` with retcode, stdout, stderr, elapsed time, peak RSS, and the isolate status/message.

3. **`Language`** (loaded from `languages.toml` via `load_languages`) — glues the two together for each language. Three shapes, chosen by which of `compiler`/`interpreter` are set:
   - interpreted (e.g. python): `interpreter` only — the interpreter runs inside isolate with the source copied in.
   - compiled native (e.g. c, cpp): `compiler` only — compile outside isolate, run the binary inside.
   - compiled + VM (e.g. java): both — `javac` outside isolate, `java` inside, with `additional_processes` added to the sandbox process budget for the JVM.

   Compiler args support `{source}`, `{output}`, `{workdir}`; interpreter args support `{executable}`, `{workdir}`, `{name}`. When adding a language, edit `languages.toml` and, if a new runtime is needed, add a package output in `flake.nix`.

### Evaluators (`cocco/judge.py`)

An `Evaluator` turns an actual output string into a `(score, message)` pair. Subclasses: `EqualityEvaluator`, `DiffEvaluator` (ratio of matching lines via `difflib.Differ`), `AccuracyEvaluator`, `F1Evaluator` (macro-F1 over the union of labels), `RMSEEvaluator`. Note that `RMSEEvaluator` returns a score where lower is better — all others are higher-is-better in `[0, 1]`.

### Data model (`cocco/model.py`)

SQLAlchemy Core `Table`s on a shared `MetaData` form the domain: `User` (with `UserRole` via `Enrollment`), `Course` → `Challenge` → `TestCase` (inputs/expected outputs referenced by filesystem path, not stored in the DB — see `DESIGN.md` "Test case inputs"; `is_sample` marks the ones students may download, `is_active` the ones new submissions are graded on), `Submission` → `TestOutcome`. There is no queue table: a submission is queued when its `status` is `SubmissionStatus.queued`. `Challenge.ranking` (a `LeaderboardRanking`) is presentation-only — it picks the leaderboard the challenge page shows and the judge never reads it.

`cocco/schema.py` holds the Pydantic mirrors used for validation and for the judge's `SubmissionRunData`.

### Queries (`cocco/repository.py`)

Every query lives here. Rather than an ORM with relationships, the page-level queries are raw SQL that build one nested JSON document with `json_agg` / `row_to_json` and return a plain dict or list — see `DESIGN.md` "Database design" for the rationale. Templates then walk that document with attribute syntax (Jinja falls back to `__getitem__`). New page queries should follow the same shape rather than issuing several round trips.

These queries are **Postgres-specific**. The judge-side helpers in the same module (`pull_queued_submission`, `insert_test_outcomes`, `mark_submission_done`) are plain Core and take a *sync* `Connection`; everything the web app uses is `async` and takes an `AsyncConnection`.

Insert helpers (`insert_enrollment`, `insert_submission`) deliberately do not commit — the caller owns the transaction. `get_db` in `main.py` yields a bare connection with no surrounding transaction, so a route that writes must `await db.commit()` itself.

### HTTP layer (`cocco/main.py`)

FastAPI app exposed as `cocco.main:app` (see `[tool.fastapi]` in `pyproject.toml`). Server-rendered Jinja templates in `templates/`, styled with Tailwind v4 + daisyUI v5 (`assets/styles.css` → `static/styles.css`), made live with htmx.

Pages return a full document; anything that needs to stay fresh is a fragment template named `f-*.jinja` served from a `/f/...` route and polled with `hx-get` + `hx-trigger="load, every Ns"`. That polling is what makes a submission visibly move from `queued` to `running` to a score without a page reload. A partial that is only ever `{% include %}`d, never served, gets a `_` prefix instead — `_statement.jinja` is the problem statement shared by the two challenge views.

One challenge URL, two pages: `get_challenge` renders `challenge-staff.jinja` for course staff and `challenge.jinja` for everyone else, and `?as=student` lets staff see what a student sees. The staff page drops the submit form and shows `query_challenge_standings` — the whole class, one row per student, each linking to `get_challenge_student` for that student's attempts.

A submission has its own page, `get_submission` at `/submissions/{id}` — flat, because the id already determines its challenge and course. It shows the source (highlighted by Pygments), the compiler's output when the submission failed to compile, and the per-test outcomes; the last two are the polled fragment `f-submission-results.jinja`, which uses `query_submission_outcomes` rather than the page's `query_submission` so a megabyte of source is not re-sent every two seconds. Both entry points the user reaches it from — the student's own history and the staff view of one student — are the same fragment, `f-submissions.jinja`, so the link is written once.

Uploads carry no language field: `post_submission` maps the file extension to a language through `EXTENSION_TO_LANGUAGE`, built from the same `languages.toml` the judge loads. Upload failures come back as **200 with an error alert**, not a 4xx, because htmx does not swap error responses by default.

### Authentication (`cocco/auth.py`)

Login is **Google OIDC**, and nothing else. `GET /login` offers one button; `GET /auth/google`
hands off to Authlib, which puts a `state`, a `nonce` and a PKCE verifier in the session and
redirects to Google; `GET /auth/google/callback` calls `authorize_access_token`, which exchanges
the code and verifies the ID token's signature, issuer, audience and nonce. There are no
passwords and no sign-up step — **the account is created the first time an accepted address
comes back**. A new user has no enrollments and is therefore read-only everywhere until course
staff enroll them.

**The gate is `is_allowed_email`, on the `email` claim.** A configured domain admits itself and
its subdomains, so `unipd.it` already covers `@studenti.unipd.it` and `@phd.unipd.it`; the suffix
test is on a dot boundary, so `notunipd.it` does not sneak in, and an **empty allowlist admits
nobody** — a deleted line in `cocco.toml` should lock the door, not open it. Google's `hd`
parameter is passed as a hint to the account chooser when `[auth] google_hd` is set and is
**never** the check: it is absent for consumer accounts, and it is not the identity.
`email_verified` is required before the address is trusted at all.

Every failure — a denied consent, a mismatched `state`, an unverified address, a domain that is
not ours, missing credentials — renders `login.jinja` with one sentence. The callback is reached
by a browser coming back from Google, so a traceback there is a dead end.

`cocco/auth.py` never touches the network: it is the config, `is_allowed_email`, `safe_next_path`,
`nickname_from_email`, `google_redirect_uri` and `get_or_create_user`. Authlib and the routes live
in `main.py`, which is what keeps the interesting half unit-testable — `test_auth_routes.py`
replaces the whole of Google with a two-method fake.

The session is a signed cookie (Starlette's `SessionMiddleware`) holding nothing but `user_id`,
so a renamed or deleted account takes effect on the next request. `same_site` must stay `"lax"`:
the return from Google is a top-level cross-site navigation, and under `"strict"` the cookie
carrying the `state` would not reach the callback, so every sign-in would fail as a state
mismatch.

**`COCCO_DUMMY_AUTH=1` bypasses all of it.** `get_user` is the only place that branches on it,
and it signs every request in as `COCCO_DUMMY_USER` (default `alice@unipd.it`) with no cookie, no
click and no credentials — creating the account if it is missing, so it works against an empty
database. `/login` and `/auth/google` merely redirect, and `/logout` is a no-op. Setting it on a
public deployment is equivalent to publishing the admin password. The seeded cast is
`alice@unipd.it` and `matteo.ceccarello@unipd.it` (teachers on every course) and `bob@unipd.it`
and `charlie@unipd.it` (students).

### Authorization

`role` lives on `enrollment`, so permissions are **per course**, not site-wide. `EDITOR_ROLES`
(teacher, admin, collaborator) is course staff; students and the unenrolled are read-only. Note
that nothing distinguishes `teacher` from `admin` today — there is no site-wide administrator in
this schema, so "may create a course" has nowhere to live yet.

Nothing is reachable without a session: every route depends on `CurrentUser`, directly or through
`require_challenge_editor`, and the only ways in are `/login`, `/auth/google`,
`/auth/google/callback` and `/static`.

The challenge editor's routes all depend on `require_challenge_editor`, which 403s a non-staff user.
Hiding a control in a template is presentation, never access control — anything that mutates a
challenge must go through that dependency. The staff view's routes depend on it too, and there it
guards *reads*: the standings and the per-student page show one student's work to someone else.

The submission page is the exception to the dependency: a student must be able to open their own
work, so `get_submission` and its fragment call `authorize_submission` instead, which allows the
author and otherwise requires `EDITOR_ROLES`.

## Conventions specific to this repo

- **Async everywhere in `judge.py`.** Subprocess calls use `asyncio.create_subprocess_exec`; tests are `async def` and rely on `asyncio_mode = "auto"` in `pyproject.toml`, so no `@pytest.mark.asyncio` is needed.
- **Box ids are a shared resource.** Always go through `_IDS_POOL.acquire()` rather than hardcoding a box id; the pool defaults to 32 concurrent boxes.
- **Large inputs live on disk.** Test case inputs/outputs are passed by path, not read into memory, and isolate takes stdin directly from a file descriptor. Preserve this when adding features — uploaded test files go through `Storage.save_stream`, never `await upload.read()`.
- **All file access goes through `Storage`.** `Storage.resolve` refuses any path that would land outside the storage root, and `safe_filename` reduces a user-supplied name to a bare, harmless one. Test case paths come from the database and ultimately from teacher input, so never join them onto a base directory by hand.
- **A test case that has graded something is never deleted, only retired.** `delete_testcase` refuses (via `count_testcase_outcomes` in the route, with the `testoutcome.testcase_id` FK as the backstop) once any outcome points at it: detaching those outcomes would leave scores in the history with nothing to say what produced them. Clearing `is_active` is the supported move — the row, the files and the past outcomes all stay, but the judge stops running it and it stops being downloadable. `is_active` is checked in `pull_queued_submission`, in `query_challenge`'s `samples` CTE and in `query_sample_testcase`; the editor's `query_challenge_testcases` deliberately does not filter it, so a retired case can be reinstated.
- **Regrading is the one place outcomes are deliberately destroyed.** `regrade_challenge` deletes every outcome of a challenge and puts every submission back to `queued`, so a fixed expected output can be applied without asking students to resubmit. Outcomes scored on *retired* test cases go too and never come back, since the judge only runs active ones — that is the difference between regrading and retiring, and it is why the confirmation dialog spells it out. Afterwards `count_testcase_outcomes` is zero for the challenge's test cases, so they are deletable again; nothing is left for a delete to strand. The route checks `count_running_submissions` first and refuses while a judge is mid-run, whose outcomes would otherwise land after the reset.
- **Language runtimes are pinned by `flake.lock`.** Changing a compiler version means updating the flake, not the system.
- **Scores are per test case, in `[0, 1]`.** A submission's headline score is the mean of its outcomes' scores, shown out of 100. This aggregation assumes higher-is-better, so it is wrong for a challenge using `RMSEEvaluator`.
- **The leaderboard's criterion is per challenge.** `challenge.ranking` is set by course staff in the editor and drives `query_challenge_leaderboard`, whose three variants live in `_LEADERBOARD_CRITERIA` as literal SQL keyed by the enum — the criterion is never taken from a request as text. `running_time` is the strict board: a submission is on it only if it passed *every* test, ranked by the sum of the per-test times. `total_score` and `average_score` rank by score, descending, and deliberately admit partially correct submissions, because a partial-credit evaluator is the case where nobody passes everything; both assume higher-is-better and so are wrong for `RMSEEvaluator`, same as the headline score. Whichever the criterion, a student appears once, represented by their best submission, and every row carries all three metrics plus `n_tests` so the fragment can render the one that applies.
- **A hidden test case gives away nothing but its verdict.** `visible_outcomes` in `main.py` blanks an outcome's `message`, `stdout` and `stderr` unless the test case is a public sample or the viewer is course staff: `stdout`/`stderr` are the submitted program's own output — the obvious way to print a hidden input back out — and `message` can be the evaluator's diff, i.e. the expected output itself (`DiffEvaluator`). The verdict, the score and the resource usage are always shown. Compiler output is exempt, and has its own block on the submission page: it does not depend on any test input. Redact in that function, not in the template, so the rule stays unit testable.
- **An anonymous htmx request gets `HX-Redirect`, not a 302.** `get_user` raises `NotAuthenticated` rather than returning a redirect — a dependency cannot return a `Response` — and the handler branches on the `HX-Request` header. This matters because most of the app is `f-*` fragments polled every few seconds: htmx follows a redirect inside the XHR, so a 302 would splice the whole login page into whatever div was polling, over and over. A 204 with `HX-Redirect` navigates the browser instead.
- **A secret never has a fixed default.** With neither `COCCO_SECRET_KEY` nor `[session].secret_key` set, `load_config` generates a random key per process and logs a warning: an unconfigured deployment then logs everybody out on every restart and no two workers agree, which is loud. A constant in the source tree would be a signing key that ships with the code, which is silent.
- **A `next` destination is filtered, never trusted.** `safe_next_path` keeps only a path on this site — one leading `/`, no scheme, no authority — so neither the `?next=` on the login page nor the one stashed in the session can be turned into an open redirect. It is applied at both ends: before the destination is stashed, and again after it is read back. The OAuth redirect URI deliberately carries no destination at all — Google matches that URI character for character against the console registration, so `next` travels in the session instead, and a callback arriving without one lands on the root.
- **CSRF rests on `same_site="lax"`, app-wide.** `POST /logout` is a form rather than a link so a planted `<img>` cannot fire it, and `lax` means the session cookie is not sent on a cross-site POST at all. That is the whole defence, and it is the same one every other mutating route in the app relies on — there is no CSRF token anywhere. A deliberate position, not an oversight: changing `same_site` would silently remove it.
- **Source is highlighted server-side.** `render_source` (the `highlight` Jinja filter) runs Pygments over a submission's source, using `submission.language` — every name in `languages.toml` happens to be a Pygments alias too — and falls back to plain text otherwise. The token colours are hand-written under `.cocco-code` in `assets/styles.css`, not a generated Pygments theme.
- **CSS is a build artifact.** Edit `assets/styles.css`, never `static/styles.css`. Tailwind only emits classes it can see, so it scans `templates/**/*.jinja` and `cocco/**/*.py` (`@source` directives); a class assembled dynamically at runtime will not be generated.
