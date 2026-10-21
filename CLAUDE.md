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
- Build the wheel and sdist: `just build`, which is `npm run build:css` *then* `uv build` — the order matters (see below)
- Drive a challenge from the command line: `COCCO_URL=http://localhost:8000 uv run cocco ls` (then `pull` / `push`)

**cocco is a package with four commands** (`[project.scripts]`): `cocco` (`cocco/cli.py`, the transfer CLI), `cocco-web` (`cocco/server.py`), `cocco-judge` (`cocco/judge.py:main`), `cocco-migrate` (`cocco/migrate.py`). `uv sync` installs it editable. Everything read at runtime — `templates/`, `static/`, `languages.toml`, `evaluators/`, `migrations/` — lives **inside `cocco/`** and is resolved from `Path(__file__).resolve().parent`, never the working directory, because an installed wheel has no repo root. There is deliberately **no `alembic.ini`** anywhere: plain `alembic` run from the checkout would pick it up and migrate whatever database *it* names instead of `cocco.toml`'s. `assets/` is tailwind input plus design-source icons; never read by Python, not shipped. `cocco/static/styles.css` is gitignored, so `[tool.hatch.build.targets.*] artifacts` names it explicitly, and it must be built before `uv build` or the wheel ships without a stylesheet.

`cocco-web` is a **thin uvicorn wrapper in its own module**, not a function in `main.py`: `cocco.main` reads its config at import (`CONFIG`, the engine, `Storage(...)`, `load_languages()` are module-level), so a `--config` flag parsed inside it would arrive too late. `server.py` sets `COCCO_CONFIG` first, then hands uvicorn the import string `"cocco.main:app"` — the only form `--reload` and `--workers` accept. `cocco-judge`'s `main` is module-level and sync because an entry point cannot name an `async def` under `if __name__ == "__main__"`.

**Tests run on PostgreSQL only**, because the app does: page queries are Postgres-specific (`json_agg`, `FILTER`, `LATERAL`), FKs and `FOR UPDATE` are enforced, and `timestamptz` exists. Don't add an SQLite path.

The suite owns **`cocco-test`**, created on demand by `cocco/conftest.py`. Host, port and user come from the environment (devShell socket or `compose.yaml` both work); `PGDATABASE` and `load_config` are deliberately **not** read, because the schema is emptied between tests and must never be a developer's `cocco-dev` or a deployment's database. The schema is built once per session with `upgrade head`; each test starts from `TRUNCATE ... RESTART IDENTITY CASCADE`, so a table's first row is id 1. `test_migrations.py` uses its own `cocco-test-migrations`, because its drift test drops the schema on the way out.

`@pytest.mark.integration` means **isolate and nix** only: `isolate` with its setuid bit, `nix` with flakes, and the user allowed to run boxes (NixOS: `security.wrappers`; elsewhere install isolate outside Nix). Everything else skips, naming `pg-start`, when no server answers.

The web app and the judge are **separate processes** that must agree on the database, `[storage] root`, and the *installed package* (`languages.toml` maps extensions at both ends; a judge on an older version grades with an older `equality.py`). The first two come from the same config file — the judge takes only `--config`, no `--database`/`--storage`, because a flag can disagree with the file. Submitting only enqueues a row; nothing leaves `queued` without a judge on the same database.

`[dev] reset_db` drops the schema and reseeds dummy data on startup. Keep it false (as `cocco.dev.toml` ships it): `--reload` would wipe the data on every file change. It discards *data*; schema changes are migrations.

**Config: the file is settings, the environment is secrets.** `cocco/config.py` reads both (its own module because the judge reads it too). The file carries `[database] url`, `[storage] root`, `[judge] flake`, `[session]`, `[auth]`, `[site]`, `[dev]`. `COCCO_SECRET_KEY`, `COCCO_GOOGLE_CLIENT_ID`/`_SECRET`, `COCCO_GITHUB_CLIENT_ID`/`_SECRET` have **no file spelling**, so there is no precedence and nothing secret in a copied file. `COCCO_CONFIG` names the file, so it is the one setting that can't live in it.

`cocco.dev.toml` is **committed** (secret-free; the Justfile points at it); `cocco.toml` is gitignored, templated by `cocco.example.toml`. **A missing file is not an error**: `DEFAULT_DATABASE_URL` is the empty `postgresql+psycopg://`, which libpq resolves from `PGUSER`/`PGDATABASE`/`PGHOST` — that keeps the devShell's `$PWD/.pg/sock` out of git and lets `cocco.main` import with no environment. Settings in their old place (`COCCO_DUMMY_AUTH`, a file `secret_key`) are **warned about, not fatal**, by `_warn_about_removed_settings`; `dummy_user` was deleted outright (`/login` asks instead). `[dev] dummy_auth` makes a fresh checkout usable; `[auth] administrators` bootstraps a deployment's first administrators.

## Architecture

Some areas have their own guide in `.claude/rules/`, loaded when you touch their files. Read the guide before changing that area, including its routes in `main.py` and queries in `repository.py`:

- `judge.md` — execution pipeline, evaluators, isolate boxes (`cocco/judge.py`, `cocco/evaluators/`)
- `migrations.md` — Alembic revisions and what autogenerate misses (`cocco/migrate.py`, `cocco/migrations/`, `cocco/model.py`)
- `auth.md` — sign-in: OAuth providers, passwords, dummy auth, tokens (`cocco/auth.py`, `passwords.py`, `tokens.py`, `config.py`)
- `transfer.md` — the `/api` zip format and the `cocco` CLI (`cocco/transfer.py`, `cocco/cli.py`)
- `exercises.md` — live Python exercises (`cocco/exercises/`, the `exercise_*` tables)
- `logging.md` — JSON logs, access log, proxy headers (`cocco/logs.py`, `cocco/server.py`)

### Data model (`cocco/model.py`)

Core `Table`s on one `MetaData`: `User` (roles via `Enrollment`; `Administrator` beside it, keyed on email so it can name someone who never signed in), `Course` → `Challenge` → `TestCase` (files referenced by path, not stored in the DB; `is_sample` = downloadable by students, `is_active` = graded), `Submission` → `TestOutcome`. No queue table: queued means `status == SubmissionStatus.queued`. `Challenge.ranking` (`LeaderboardRanking`) is presentation-only; the judge never reads it. The `exercise_*` tables share nothing with this above the execution layer.

`User.blocked_at` / `blocked_reason` are the **blacklist**, columns on the row (a block always names an existing account), so `get_user` sees it on every request and a block bites on the next click. `_USER_COLUMNS` in `repository.py` is the single column list behind `query_user` and `query_user_by_email`, so no door can forget `blocked_at`.

**Every timestamp is `timestamptz` written by Postgres** (`server_default=func.now()`), never `datetime.now()`: web and judge have two clocks, `now()` has one (the transaction's). `created_at()` / `updated_at()` at the top of `model.py` are factories (a `Column` belongs to one `Table`): both on `user`, `course`, `enrollment`, `challenge`, `exercise_set`; `created_at` alone on `administrator`, `testcase`, `exercise_set_version`; `updated_at` alone on `exercise_session` (created = `opened_at`). `updated_at` is a blanket `onupdate` meaning only "row changed". `set_enrollment_role` sets it by hand, because `onupdate` doesn't fire for an upsert's DO UPDATE.

- **`user.first_login_at` ≠ `created_at`**: `get_or_create_user` also runs from the people page (rosters before term), so NULL means "never signed in". `record_sign_in` sets it once via `coalesce`, alongside `last_login_at`, from `_complete_sign_in` *after* the blacklist check.
- **`submission.started_at` / `finished_at`** bracket a judge run (`pull_queued_submission`, `mark_submission_done`) and `regrade_challenge` resets them to NULL. `submission.enqueued` is that table's `created_at`.
- Neither goes through `_USER_COLUMNS`, which stays the list `schema.User` is built from.

`cocco/schema.py` holds the Pydantic mirrors (including the judge's `SubmissionRunData`). `schema.User`'s block fields have **no defaults**, so a query with a trimmed column list fails validation instead of reporting everyone as unblocked.

### Queries (`cocco/repository.py`)

Every query lives here. Page-level queries build one nested JSON document with `json_agg` / `row_to_json`, returning a dict or list that templates walk with attribute syntax (`DESIGN.md` "Database design"). New page queries follow that shape rather than making several round trips. They are **Postgres-specific**. A shared rule or CTE is a small function returning a Core object (`_course_challenges`, `_session_checks`, …), its dependencies passed as arguments, never a string spliced into `text()`. Page queries that share nothing may still be `text()`; the leaderboard's `_LEADERBOARD_CRITERIA` is enum-keyed literal SQL by design. Judge-side helpers (`pull_queued_submission`, `insert_test_outcomes`, `mark_submission_done`) are plain Core on a *sync* `Connection`; web-side ones are `async` on an `AsyncConnection`.

Insert helpers (`insert_enrollment`, `insert_submission`) don't commit; the caller owns the transaction. `get_db` yields a bare connection with no transaction, so a writing route must `await db.commit()`.

### HTTP layer (`cocco/main.py`)

FastAPI app `cocco.main:app`; Jinja templates in `cocco/templates/`, Tailwind v4 + daisyUI v5 (`assets/styles.css` → `cocco/static/styles.css`), htmx.

Pages return full documents. Anything live is an `f-*.jinja` fragment served from `/f/...` and polled with `hx-get` + `hx-trigger="load, every Ns"` (that's how a submission moves `queued` → `running` → score). An include-only partial gets a `_` prefix (e.g. `_statement.jinja`, shared by both challenge views).

**htmx rules used throughout:**
- Errors a user should see are **200 with an alert**, not 4xx: htmx doesn't swap error responses. This stops at htmx: `/api` routes refuse with real status codes and JSON, so a CLI can't mistake a refusal for success.
- A form field that may be empty is a **defaulted** `Form` field, not required: a missing field is a 422, whose body htmx drops (e.g. `post_administrator`'s `email`).
- A filter input sits **outside** the element it swaps (a rebuilt input loses focus), and so does any form beside a filtered list (the filter would wipe a half-typed value). Every mutation on a filtered list carries `q`.
- A page with mutations has **one swap target**, and every route on it answers with the same fragment, alert included, so message and new state arrive in one swap: `/admin` → `#users` / `f-users.jinja`; people page → `#people` / `f-course-people.jinja`; profile → `#identity`.
- Navigation after a mutation is `204` + `HX-Redirect`.

**Route order is load-bearing.** `/courses/{course_id}` would swallow `/courses/new`, and `/courses/{course_id}/{challenge_id}` matches any single segment. So `/courses/new` is declared above `get_course`, and every other two-segment course page (`/edit`, `/people`, the course danger-zone routes, exercises and sessions) above `get_challenge`. `/admin` collides with nothing.

**Challenge and submission pages.** One URL, two pages: `get_challenge` renders `challenge-staff.jinja` for staff (with `query_challenge_standings`, linking to `get_challenge_student`) and `challenge.jinja` otherwise; `?as=student` shows staff the student view. `get_submission` is flat at `/submissions/{id}`, since the id determines the challenge. It shows Pygments-highlighted source, compiler output and per-test outcomes. The last two are the polled `f-submission-results.jinja` using `query_submission_outcomes` (not `query_submission`), so the source isn't re-sent every poll. Both the student history and the staff per-student view list submissions via the same `f-submissions.jinja`.

Uploads carry no language field: `post_submission` maps the extension through `EXTENSION_TO_LANGUAGE`, built from `languages.toml`.

**Home page** (`index`, `index-logged-in.jinja`): enrolled courses, then everything else (`query_courses` splits on `role IS NULL`), both as `_course-card.jinja`, the deliberate twin of `_challenge-card.jinja`. The filter `q` matches the **title** with `position()`, not `ILIKE`, so `%` and `_` are literal with no escaping. It swaps `#courses` with `f-courses.jinja` (both halves), which the page also inlines on first paint.

**Enrolling yourself** is `post_course_enroll`. On a home-page card it re-renders the courses fragment (with `q`) so the course moves to the top half. On the course preview it sends `next` (through `safe_next_path`) and gets an `HX-Redirect` back. Its authorization is `CurrentUser` only: it is **the only route under a course a non-member may call**. So the role is fixed inside `insert_self_enrollment` (no form field can spell `teacher`), and its `ON CONFLICT DO NOTHING` makes double clicks no-ops and never demotes an existing teacher. There is no self-unenroll.

**`course.enrollment_key`**: NULL = anyone signed in may join; otherwise `post_course_enroll` compares it with `hmac.compare_digest` on the trimmed string before writing. It's stored and shown **in plaintext** on purpose (staff read it out to the class; it guards a class list, not an account), so the **course editor is the only place it's printed**. `query_courses` / `query_course` expose only `requires_key` (`enrollment_key IS NOT NULL`) and list columns rather than `SELECT *`; `query_enrollment_key` is the single read. The key field lives only on the course preview (`f-enroll.jinja`, re-swapped with the message on a bad key); a keyed course's card shows an `Enroll…` link to it.

**Course creation** is `/courses/new` (`course-new.jinja`), button shown only to administrators; on success → the new course's editor.

**`/admin`** (`admin.jinja`, behind `Admin`) lists users, appoints administrators and blocks accounts. Blocking is an inline per-row form with a reason, not a `<dialog>`: it's reversible, and N dialogs would be rebuilt on every filter keystroke.

**Course editor** `/courses/{course_id}/edit`: title, description (same markdown textarea and `post_markdown_preview` as challenges), and the challenge list reordered with ↑/↓. Cards are `_challenge-card.jinja`, shared with the course page via `f-course-challenges.jinja`, so the two lists can't drift.

**Danger zones** end both editors: archive (reversible) and delete (not).
- The course's is **`Admin` only**, not `CourseEditor`: what exists site-wide is the site's business. `get_course_edit` passes `is_admin` to draw it; the routes check for themselves.
- The challenge's is behind `ChallengeEditor` (admins + staff, with the `challenge_in_course` cross-check).
- Archive/unarchive are two routes (like `/block` / `/unblock`). All six answer `HX-Redirect`: an archive goes back to the same editor, a deleted challenge to the course editor, a deleted course to the home page.
- **A delete asks for the title, typed out**: a `<dialog class="modal">` whose inline script only *enables* the button on a match. `_confirmation_matches` re-checks in the route, because a disabled button is presentation. It uses a plain `==` on the trimmed string: the title is on the page, so this confirms intent rather than proving a secret. The title reaches the script via **`data-expected`** (Jinja attribute escaping), never a JS string literal (`Dijkstra's` would break it).
- Both refuse with 200 + `f-edit-result.jinja` on a wrong name or while a judge is mid-run.

**Archiving is visibility, not a lock**, with exactly **three enforcement points**: `post_submission`, `post_course_enroll`, and the exercise Run/Check routes (`_post_session_job`, `_post_exercise_preview_job`). Exercise *builds* are allowed, because importing is editing. An archived course or challenge stays readable to those enrolled, stops taking submissions, leaves other people's listings and admits nobody new. Staff keep editing it (via the web or `cocco push`) and unarchive it from the same place.

**People page** `/courses/{course_id}/people` (`course-people.jinja`): the roster, roles, submission counts here, and staff's three mutations. It is behind `CourseEditor` for reads too, because a roster lists real people. Enrolling takes an **address** (rosters predate sign-in): `enrollment` is keyed on `user.id`, so the account is **created** via `get_or_create_user`, the same call the OAuth callback uses. So pre-enrolled people are ordinary `user` rows on `/admin`. A *blocked* account is **not** refused here (unlike appointing an admin), because a block stops a person without retracting their place on a course.
- **You cannot remove yourself** (checked in the route).
- **A course never loses its last teacher**: checked inside `set_enrollment_role`'s `ON CONFLICT` and `delete_enrollment`'s `WHERE`. It is sound only because every mutation takes `lock_course` (`FOR UPDATE` on the course row) first; under READ COMMITTED, two concurrent demotions would otherwise both succeed.
- `Remove` on someone who has submitted asks via `hx-confirm`, not a `<dialog>`, because nothing is destroyed.

**Profile** `/users/{user_id}` (`profile.jinja`): one URL, two pages (forms for the owner, text for an administrator).
- `require_profile_reader` allows owner or admin and **returns `can_edit`** (like `authorize_submission`'s `is_staff`). `require_profile_owner` has no admin branch: reading is oversight, renaming is impersonation.
- Course staff are strangers here, because the page spans every course.
- The address is editable by nobody: it's the identity a provider proves.
- `update_user` is the write; blocks go through `block_user`, whose `WHERE` carries a rule `update_user` would lose.
- Mutations answer `#identity` from `query_user` (plain Core). The address, badges and course list sit outside it, keeping the Postgres-only `query_user_profile` out of the mutation path.
- A rename also swaps `_navbar-identity.jinja` **out of band**, so the navbar can't go stale.
- **Avatar** is `user.image`, a blob whose type comes from `sniff_image_mime`, **never** the upload's `content_type`, because `user_avatar` serves the bytes from this origin. SVG is refused (we generate identicons, not uploads). The response has `nosniff`, a `default-src 'none'` CSP and `Cache-Control: no-cache` (the URL doesn't change with the picture).
- `Generate from my name` reseeds `generate_identicon` from the current nickname; a rename never redraws an avatar by itself.

### Authorization

`role` lives on `enrollment`, so permissions are **per course**. `EDITOR_ROLES` (teacher, collaborator) is course staff; students read and submit. The only site-wide power is the **administrator**, deliberately not a `UserRole`.

**Enrollment gates a course's contents.** Non-members are shut out, not read-only:
- `require_course_member` 403s anyone with no role on the path's course.
- `require_challenge_viewer` adds the `challenge_in_course` cross-check (as `require_challenge_editor` does), so nobody can name their own course in front of another's challenge.
- `require_challenge_member` is the challenge-only-URL variant (`/f/submissions/{id}`, `/f/leaderboard/{id}`, sample download); with one id there's nothing to disagree with.
- Staff of one course are strangers on every other.

**Outside the gate**, deliberately: the home page and `/f/courses` (or nobody could find a course), `post_course_enroll` (the way in), and `get_course`, which for a non-member renders a **preview** (title, description, `Enroll!`) and branches *before* `query_course_info` runs.

**Two doors into a course**: `post_course_enroll` (self, role fixed to student) and the people page (staff, any role, can remove). `set_enrollment_role` is the write for the latter; `insert_enrollment` survives only for `post_course_new`.

**Refusals are pages.** `Forbidden` is an `HTTPException` subclass carrying the sentence; `handle_forbidden` (registered on that subclass alone) renders `403.jinja`. Handlers can't take dependencies, so `get_user` stashes `request.state.user` for the navbar. Polled fragments are unaffected (htmx ignores error bodies).

**Site administrators** are rows in `administrator`, keyed on *email* so they can be named before first sign-in. `is_administrator` is the single read; `require_admin` (`Admin`) guards course creation, `/admin` and the course danger zone, and its message names the permission ("Only a site administrator can do that."). A course's creator is also enrolled as `teacher`. `[auth] administrators` is a **bootstrap**: `create_db_and_tables` replays it each boot with `ON CONFLICT DO NOTHING`. It only adds; removing a line demotes nobody.

Appointing takes an address; `/admin` and the people page are the **only two** places a human types one, and both call `normalize_email` and refuse addresses outside `allowed_domains` (`_load_auth_config` only *warns* for the config file, so a typo can't stop boot). **There is no revoke** (it's a `DELETE`), so two guards keep an unremovable, unusable admin from existing: **an administrator is never blocked** (in `block_user`'s `WHERE`, race-free) and **a blocked address is never appointed**. This also protects the seeded dev admin. Escape hatches: `DELETE FROM administrator WHERE email = '…';` and `UPDATE "user" SET blocked_at = NULL WHERE email = '…';`

**Blocks are enforced in `get_user`** and again in `_complete_sign_in` (shared by every sign-in path, dummy auth included), so a blocked person sees a sentence instead of a session that dies on the first click. `Blocked` subclasses `Forbidden`; `handle_blocked` renders `blocked.jinja` with **no viewer** (the navbar avatar is behind `CurrentUser` and would 403), which is why `get_user` refuses *before* stashing `request.state.user`. The session is **not** cleared, and polled fragments just 403. It is deliberately not the `HX-Redirect` that `handle_not_authenticated` sends, which would look like being signed out.

**Administrators are staff on every course**, in exactly one place: `effective_course_role` / `effective_challenge_role` return the enrollment's role, or `teacher` for an admin (only looked up when there is no enrollment). So `EDITOR_ROLES` stays per-course and every `role in EDITOR_ROLES` keeps working. Consequences: `get_course` gives admins the course, not the preview, and `get_challenge` takes `can_edit` from the dependency, not from `query_challenge`'s `viewer_role` (a `LEFT JOIN`, NULL for admins).

**Nothing is reachable without an identity**: every route depends on `CurrentUser` or `ApiUser`, directly or via `require_challenge_editor` / `require_api_challenge_editor`. The only open paths are `/login`, `/auth/{provider}`, `/auth/{provider}/callback`, `/static`. `ApiUser` is the only non-session identity (bearer tokens for the CLI; see `transfer.md`).

**Hiding a control is presentation, never access control**: anything mutating a challenge goes through `require_challenge_editor`, which also guards the staff view's *reads* (standings, per-student pages). The exception is the submission page: `get_submission` and its fragment use `authorize_submission` (author, else `EDITOR_ROLES`). It needs no membership check, since `post_submission` was gated.

## Conventions specific to this repo

- **Box ids are a shared resource.** Always go through `_IDS_POOL.acquire()`, never a hardcoded id; the pool defaults to 32.
- **Language runtimes are pinned by `flake.lock`.** Change a compiler by updating the flake, not the system.
- **The judge's own conventions** (async subprocesses, compressed storage vs plain boxes, the flat box directory, compile-under-the-uploaded-name) are in `.claude/rules/judge.md`.
- **Large inputs live on disk.** Test files are passed by path and reach a box in chunks. Uploads go through `Storage.save_stream`, never `await upload.read()`; files leaving the app (sample download, export zip) are streamed. **Exercises are the deliberate exception — don't "fix" it**: their inputs are a few typed bytes stored in the version's compiled document, read in one query (`query_job_exercise`), with no `Storage` involved.
- **All file access goes through `Storage`.** `Storage.resolve` refuses paths outside the root, and `safe_filename` reduces a user-supplied name to a bare one. Test case paths come from teacher input; never join them onto a directory by hand.
- **Everything under the storage root is gzip, under its logical name**, and only `cocco/storage.py` knows. `write`, `read`, `open`, `save_stream` take and return plaintext.
  - **No `.gz` suffix**: `testcase.input_path` stays the uploaded name used by `resolve`, `rename`, `plan_testcases`, etc. A suffix would need adding and stripping everywhere, and silently break wherever it was forgotten.
  - `save_stream` returns the **uncompressed** count (what `MAX_TESTCASE_BYTES` limits); `size` is bytes on disk.
  - Deflating is CPU, so async callers use a thread (`post_testcase`, the push's `store`, the export's zip build).
  - Files leave as decompressing **streams**, never `FileResponse` (`_stored_file_download`, `_zip_stored`).
  - No plaintext fallback: `cocco-migrate compress-storage` converts a root, `check_storage_format` refuses to start without the `.storage-format` marker (in `server.py`, the startup hook and `judge.main`), and reading a plain file raises `StorageFormatError` naming that command.
  - Bundled evaluators in `cocco/evaluators/` are source tree, so plain: `prepare_evaluator` and `get_evaluator_download` each keep that one branch.
- **`challenge.position` orders, `number` numbers.** Order is `position, id` everywhere. The printed "Challenge 03" is `number`, the rank `_challenge_number()` computes, because reorders leave gaps. `reorder_challenges` renumbers the whole course densely rather than swapping (like `swap_testcase_order` does), because `position` defaults to 0 and swapping two zeroes moves nothing.
- **The number on a challenge card comes off the row, never `loop.index`.** `_course_challenges` computes `number` over the whole course *before* `_listed_challenges` drops hidden ones, so a student's filtered list prints the same numbers staff see.
- **A test case that has graded something is never deleted, only retired** (`is_active` cleared; row, files and outcomes stay). `delete_testcase` refuses via `count_testcase_outcomes`, with the `testoutcome.testcase_id` FK as backstop. `is_active` is checked in `pull_queued_submission`, `query_challenge`'s samples and `query_sample_testcase`. The editor's `query_challenge_testcases` deliberately doesn't filter it, so a retired case can be reinstated.
- **Regrading is the one place outcomes are deliberately destroyed.** `regrade_challenge` deletes every outcome of the challenge, including those on retired cases, which never come back (the dialog says so), and requeues every submission. Afterwards its test cases are deletable again. `post_regrade` refuses while `count_running_submissions` > 0; that race costs at most one stale score.
- **Deleting a challenge is the one place a submission is destroyed, and the cascade is hand-written.** No FK declares `ON DELETE`, deliberately (`testoutcome.testcase_id` having none is what backstops `delete_testcase`).
  - `delete_challenge` deletes testoutcome by `submission_id`, then by `testcase_id` (both FKs are nullable), then submission, testcase, challenge.
  - `delete_course` calls it per challenge, plus `delete_exercise_session` / `delete_exercise_set`, then enrollments and the course.
  - Files are read **before** (`query_challenge_file_paths`) and unlinked **after** commit, so a failed commit leaves an orphan file, never a dangling row. Directories go through `Storage.delete_directory` (`rmdir`, never `rmtree`).
  - A delete locks with `lock_challenge_submissions` / `lock_course_submissions` (`FOR UPDATE` plus count), because the judge's `run_from_queue` writes outcomes with no `except`, and a mid-run delete fails that job on an FK. Locks go on `submission` before `testoutcome`, the judge's order, so they can't deadlock.
- **"Archived" is one `or` (challenge *or* its course), spelled once**: `_effective_archived_at()`, read by page queries and single-row reads alike; `test_repository.py` checks every reading agrees.
  - Page queries expose it as **`effective_archived_at`**, never `AS archived_at` (a duplicate key in `row_to_json` is last-one-wins), and templates read that.
  - Only the challenge editor's danger zone reads `challenge.archived_at` itself (to explain "its course is archived"). Anywhere else that silently un-archives challenges under archived courses.
  - `query_courses` hides archived courses from the non-enrolled half only; `include_archived` lets administrators find them.
- **"Visible" is `challenge.visible_from`**: NULL = hidden, past = visible, future = opens by itself.
  - Spelled once, `_challenge_visible()`, with a behavioural test in `test_repository.py`. Unlike archiving it does **not** cascade from the course.
  - Enforced in **one** place: `require_challenge_visible`, called from `require_challenge_viewer` and `require_challenge_member`, so routes (including `post_submission`) need no check of their own.
  - Hidden → bare 404; scheduled → `Forbidden` naming the date, matching its teaser card.
  - `insert_challenge` writes NULL, so **new challenges are hidden**.
  - `_course_challenges` exposes **`is_visible`** (never `AS visible`); its callers' `include_hidden` defaults to the student's list.
  - It **is** a `challenge.toml` key (absent = hidden, so a pushed draft stays a draft).
- **The judge never learns about archived, hidden or blocked.** `pull_queued_submission` doesn't filter on any of them: skipping rows would strand them as `queued` forever, polluting standings and the regrade guard.
- **A block stops a person, it doesn't retract their work**: queued submissions are graded, and standings and leaderboards keep listing them. Nothing blocked is seeded (a blocked seeded admin would lock out a fresh checkout). **Removing an enrollment draws the same line**: `delete_enrollment` takes one row. Submissions and outcomes stay, because `query_challenge_standings`'s cohort already unions in everyone who submitted.
- **Scores are per test case, in `[0, 1]`**; the headline is their mean ×100. That assumes higher-is-better, and nothing enforces the range.
- **The leaderboard criterion is per challenge**: `challenge.ranking` drives `query_challenge_leaderboard`, selecting one of `_LEADERBOARD_CRITERIA`'s literal SQL variants by enum, never request text. `running_time` admits only submissions passing *every* test, ranked by summed time. `total_score` / `average_score` rank by score and admit partial credit. Each student appears once (best submission), and every row carries all three metrics plus `n_tests`.
- **A hidden test case gives away nothing but its verdict.** `visible_outcomes` in `main.py` blanks `message`, `stdout`, `stderr` unless the case is a sample or the viewer is staff: stdout/stderr can echo the hidden input, and `message` comes from an evaluator that read the hidden input and expected output (`diff.py` prints the expected output). Verdict, score and resource usage are always shown, and compiler output is exempt. Redact in that function, not templates, so it stays unit-testable.
- **An anonymous htmx request gets `HX-Redirect`, not a 302.** `get_user` raises `NotAuthenticated` (a dependency can't return a `Response`), and the handler branches on `HX-Request`. A 302 would be followed inside the XHR and splice the login page into every polling div.
- **A secret has one source and never a fixed default.** Secrets come from the environment only (see Commands). With `COCCO_SECRET_KEY` unset, `load_config` generates a random per-process key and warns: loud (logouts on restart, workers disagree) rather than a silent signing key shipped in the code.
- **A `next` destination is filtered, never trusted.** `safe_next_path` keeps only an on-site path (one leading `/`, no scheme or authority), applied both before stashing and after reading back. The OAuth redirect URI carries no destination (providers match it exactly), so `next` travels in the session; without one the callback lands on `/`.
- **CSRF rests entirely on `same_site="lax"`**: there's no CSRF token anywhere, and `POST /logout` is a form so an `<img>` can't fire it. Changing `same_site` silently removes the defence.
- **Source is highlighted server-side**: `render_source` (the `highlight` filter) runs Pygments on `submission.language` (every `languages.toml` name is a Pygments alias), falling back to plain text. Token colours are hand-written under `.cocco-code` in `assets/styles.css`.
- **CSS is a build artifact.** Edit `assets/styles.css`, never `cocco/static/styles.css`. Tailwind only emits classes it sees in `cocco/templates/**/*.jinja` and `cocco/**/*.py` (`@source`), so never assemble class names at runtime. `cocco/static/` holds only files templates name: `icon-small.png`, `favicon.ico`, `styles.css`, plus `exercise.js` and CodeMirror 5 under `codemirror/` (vendored with its MIT `LICENSE`, excluded via `@source not`). Large icon sources stay in `assets/`.
- **A static URL carries its own version.** `static_url()` appends the file's mtime as `?v=`; `VersionedStaticFiles` serves versioned URLs with a year of `immutable` and unversioned ones with `no-cache` (bare `StaticFiles` caused a navbar flicker by revalidating every load).
  - The mtime is read **per render, not at import**, because `npm run watch:css` changes no `.py`. A missing file falls back to the bare path.
  - `STATIC_DIR` and the `/static` mount must name **one** directory.
  - The logo is **preloaded above the stylesheet**, and `base.jinja` binds `logo_url` once for both uses, because a one-byte href mismatch fetches it twice.
