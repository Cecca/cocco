# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

`cocco` is a self-hosted judge for programming challenges. Submissions are executed inside the [isolate](https://github.com/ioi/isolate) sandbox (the IOI/ICPC judge), using language runtimes supplied by a Nix flake. The HTTP layer is FastAPI with Jinja templates and htmx; persistence is the SQLAlchemy 2.0 Core API on PostgreSQL.

`DESIGN.md` has the rationale for the isolate + Nix choice and the alternatives that were rejected; read it before proposing architectural changes.

## Commands

Dependencies are managed with `uv` (Python 3.13). A `.envrc` / Nix `devShell` auto-creates `.venv` and activates it.

- Install deps: `uv sync`
- Start / stop PostgreSQL: `pg-start`, `pg-stop` (then `create-db cocco-dev` once)
- Run the whole stack (app, judge, Postgres): `just dev`
- Run the API alone: `COCCO_CONFIG=cocco.dev.toml uv run fastapi dev cocco/main.py`
- Run the judge alone: `COCCO_CONFIG=cocco.dev.toml uv run python -m cocco.judge`
- Reset and reseed the database: set `[dev] reset_db = true`, start once, set it back
- Configure a deployment: `cp cocco.example.toml cocco.toml` and edit it — the three secrets go in the environment, not in it
- Run all tests: `uv run pytest`
- Run only unit tests (skip those requiring isolate + nix + postgres): `uv run pytest -m "not integration"`
- Run a single test: `uv run pytest cocco/test_judge.py::test_language_python`
- Build a language runtime on its own: `nix build .#python3` (or `.#gcc`, `.#jdk`)
- Build the CSS: `npm install` once, then `npm run build:css` (or `npm run watch:css`)

Integration tests (`@pytest.mark.integration`) require `isolate` installed with its setuid bit, a working `nix` with flakes, the current user being allowed to run isolate boxes, and — for the repository tests — a running PostgreSQL. On NixOS, isolate is declared via `security.wrappers`; on other distros install it outside Nix. Note that the repository integration test **drops every table** in `$PGDATABASE` — which is why the `postgres_url` fixture in `cocco/conftest.py` builds its URL from the `PG*` variables and deliberately **not** from `load_config`: a deployment's `cocco.toml` names that deployment's database, and `pytest -m integration` must have no way of finding it.

The web app and the judge are **separate processes** that must agree on three things: the database, the storage root for test case files (`[storage] root`), and the checkout itself — `languages.toml` decides what an uploaded extension means at both ends, and the bundled evaluators in `cocco/evaluators/` are code, so a judge running an older checkout silently grades with an older `equality.py` than the editor advertises. The first two they now agree on by *reading the same file*: the judge takes no `--database` and no `--storage`, only a `--config`, because a flag that can disagree with the file is the same bug as an environment variable that can. Submitting through the web UI only enqueues a row — nothing leaves `queued` unless a judge is running against the same database.

`[dev] reset_db` drops every table and reseeds dummy data on startup. Leave it false in normal development — `cocco.dev.toml` ships it that way — because `fastapi dev` reloads on every file change and each reload would wipe the data. There is no migration tool, so a schema change reaches an existing database only through one such reset.

**Configuration is split in two, and the line is one sentence: the file is settings, the environment is secrets.** `cocco/config.py` is where both halves are read — its own module rather than a corner of `auth.py`, because the judge reads it too and a database URL is none of sign-in's business. The file carries `[database] url`, `[storage] root`, `[session]`, `[auth]`, `[site]` and `[dev]`; the three secrets — `COCCO_SECRET_KEY`, `COCCO_GOOGLE_CLIENT_ID`, `COCCO_GOOGLE_CLIENT_SECRET` — have **no file spelling at all**, so there is no precedence to reason about and nothing secret in a file that gets copied, diffed and pasted into a bug report. `COCCO_CONFIG` is the exception that proves the rule: it names the file, so it is the one setting that cannot be written in one.

There are **two files**, and which one you get is `COCCO_CONFIG`. `cocco.dev.toml` is **committed**: it is secret-free by construction, and `just dev` points at it. `cocco.toml` is gitignored and is a deployment's own; `cocco.example.toml` is its commented template. **A missing file is still not an error** — and now says more than it did, because `DEFAULT_DATABASE_URL` is the empty `postgresql+psycopg://`, which libpq resolves from `PGUSER`/`PGDATABASE`/`PGHOST`. That is deliberate and is what lets a file in git avoid naming the devShell's socket, whose path is `$PWD/.pg/sock`. It also means importing `cocco.main` needs no environment and no file at all, so the unit tests run outside the dev shell. A setting left in its old place — a `COCCO_DUMMY_AUTH` in the shell, a `secret_key` in the file — is **warned about, not ignored**: `_warn_about_removed_settings` names the old spelling and the new one, and only warns, because a stale variable must not stop the app from booting. `[dev] dummy_auth` is what makes a fresh checkout usable at all, and `[auth] administrators` is where a deployment's first administrators are listed (a bootstrap — see "Authorization").

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

SQLAlchemy Core `Table`s on a shared `MetaData` form the domain: `User` (with `UserRole` via `Enrollment`, and `Administrator` beside it — every `UserRole` is a *per-course* thing, so the site-wide power to create a course is its own table, keyed on the email so it can name someone who has never signed in), `Course` → `Challenge` → `TestCase` (inputs/expected outputs referenced by filesystem path, not stored in the DB — see `DESIGN.md` "Test case inputs"; `is_sample` marks the ones students may download, `is_active` the ones new submissions are graded on), `Submission` → `TestOutcome`. There is no queue table: a submission is queued when its `status` is `SubmissionStatus.queued`. `Challenge.ranking` (a `LeaderboardRanking`) is presentation-only — it picks the leaderboard the challenge page shows and the judge never reads it.

`User.blocked_at` / `blocked_reason` are the **blacklist**, and they are columns on the row rather than a table of their own — deliberately the opposite of `Administrator`, and for the opposite reason. That table is keyed on an address because it must name people who have never signed in; a block always names someone who already did something, so the account exists. Living on the row means `get_user` — which already reads it on every request — asks the question for free, and a block therefore takes effect on the blocked person's very next click. `_USER_COLUMNS` in `repository.py` is the one column list behind both `query_user` and `query_user_by_email`, spelled once because a list that carried `blocked_at` in one of them and not the other would let a blocked person in through whichever door forgot.

`cocco/schema.py` holds the Pydantic mirrors used for validation and for the judge's `SubmissionRunData`. `schema.User` carries the two block fields **without defaults**: `get_user` refuses on them, so a query that selected a trimmed column list has to fail loudly at validation rather than quietly report everybody as welcome.

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
Google callback uses. Two consequences worth knowing. A pre-enrolled person gets a real `user`
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
The address is nobody's to edit, including its owner's: it is the login identifier Google proves.
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

`cocco/auth.py` never touches the network: it is `is_allowed_email`, `safe_next_path`,
`nickname_from_email`, `google_redirect_uri` and `get_or_create_user`. Authlib and the routes live
in `main.py`, which is what keeps the interesting half unit-testable — `test_auth_routes.py`
replaces the whole of Google with a two-method fake. The configuration is not here either: it is
`cocco/config.py`, because the judge reads the same file and a database URL is none of sign-in's
business. The dependency runs one way — `config.py` imports `is_allowed_email` and
`normalize_administrators` from here, and nothing here imports `config.py`.

The session is a signed cookie (Starlette's `SessionMiddleware`) holding nothing but `user_id`,
so a renamed or deleted account takes effect on the next request. `same_site` must stay `"lax"`:
the return from Google is a top-level cross-site navigation, and under `"strict"` the cookie
carrying the `state` would not reach the callback, so every sign-in would fail as a state
mismatch.

**`[dev] dummy_auth` bypasses all of it.** `get_user` is the only place that branches on it,
and it signs every request in as `[dev] dummy_user` (default `matteo.ceccarello@unipd.it`, the
seed's administrator, so a fresh checkout can create a course) with no cookie, no
click and no credentials — creating the account if it is missing, so it works against an empty
database. `/login` and `/auth/google` merely redirect, and `/logout` is a no-op. Setting it on a
public deployment is equivalent to publishing the admin password. The seeded cast is
`matteo.ceccarello@unipd.it` and `alice@unipd.it` (`teacher` on every course) and `bob@unipd.it`
and `charlie@unipd.it` (students). Matteo and Alice differ in one thing only: he is a **site
administrator**, a row in `administrator` rather than a role anywhere, which is why a fresh
checkout can create a course. `create_db_and_tables` seeds him whenever dummy auth is on — a file
every checkout shares should not name one person as the administrator of all of them, so
`cocco.dev.toml` deliberately lists none and this is what keeps the checkout usable — and
deliberately seeds `DUMMY_ADMIN` rather than `DUMMY_USER`, so `dummy_user = "alice@unipd.it"`
still shows what plain course staff may do instead of quietly promoting her.

`main.DUMMY_AUTH` and `main.DUMMY_USER` are derived from `CONFIG.dev` **once, at import, and stay
module-level names** — `get_user` and `create_db_and_tables` read those rather than `CONFIG.dev`,
because that is the seam five test files sign themselves in through, some of them switching who is
signed in halfway through a test. A frozen dataclass field has no equivalent, so this is not a
tidy-up waiting to happen.

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
also what keeps `just dev` safe: `[dev] dummy_user` defaults to the seeded administrator. The two
escape hatches belong side by side: `DELETE FROM administrator WHERE email = '…';` and
`UPDATE "user" SET blocked_at = NULL WHERE email = '…';`

**The block is enforced in `get_user`, on both branches** — including the `DUMMY_AUTH` one, or
dummy auth would be the single configuration in which the feature does not exist, which is the
configuration it gets developed in — and again in the Google callback, so a blocked person gets a
sentence on the page they are looking at instead of a session that dies on the first click.
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

Nothing is reachable without a session: every route depends on `CurrentUser`, directly or through
`require_challenge_editor`, and the only ways in are `/login`, `/auth/google`,
`/auth/google/callback` and `/static`.

The challenge editor's routes all depend on `require_challenge_editor`, which 403s a non-staff user.
Hiding a control in a template is presentation, never access control — anything that mutates a
challenge must go through that dependency. The staff view's routes depend on it too, and there it
guards *reads*: the standings and the per-student page show one student's work to someone else.

The submission page is the exception to the dependency: a student must be able to open their own
work, so `get_submission` and its fragment call `authorize_submission` instead, which allows the
author and otherwise requires `EDITOR_ROLES`. It needs no membership check of its own — the author
was enrolled when they submitted, because `post_submission` is behind the gate too.

## Conventions specific to this repo

- **Async everywhere in `judge.py`.** Subprocess calls use `asyncio.create_subprocess_exec`; tests are `async def` and rely on `asyncio_mode = "auto"` in `pyproject.toml`, so no `@pytest.mark.asyncio` is needed.
- **Box ids are a shared resource.** Always go through `_IDS_POOL.acquire()` rather than hardcoding a box id; the pool defaults to 32 concurrent boxes.
- **Large inputs live on disk.** Test case inputs/outputs are passed by path, not read into memory, and isolate takes stdin directly from a file descriptor. Preserve this when adding features — uploaded test files go through `Storage.save_stream`, never `await upload.read()`.
- **All file access goes through `Storage`.** `Storage.resolve` refuses any path that would land outside the storage root, and `safe_filename` reduces a user-supplied name to a bare, harmless one. Test case paths come from the database and ultimately from teacher input, so never join them onto a base directory by hand.
- **`challenge.position` orders, `number` numbers.** The column is the sort key the course editor's arrows write, ordered by `position, id` everywhere so challenges nobody has reordered keep the order they were created in. What a page prints ("Challenge 03") is `number`, the *rank* of that key within the course, computed in `query_challenge` — the two are separate fields because a reorder leaves gaps and skipped numbers would be a bug. Moving one is `reorder_challenges`, a dense renumber of the whole course rather than the sentinel swap `swap_testcase_order` does: `position` defaults to 0, so every row of a course that predates the column starts out tied, and exchanging two zeroes moves nothing.
- **A test case that has graded something is never deleted, only retired.** `delete_testcase` refuses (via `count_testcase_outcomes` in the route, with the `testoutcome.testcase_id` FK as the backstop) once any outcome points at it: detaching those outcomes would leave scores in the history with nothing to say what produced them. Clearing `is_active` is the supported move — the row, the files and the past outcomes all stay, but the judge stops running it and it stops being downloadable. `is_active` is checked in `pull_queued_submission`, in `query_challenge`'s `samples` CTE and in `query_sample_testcase`; the editor's `query_challenge_testcases` deliberately does not filter it, so a retired case can be reinstated.
- **Regrading is the one place outcomes are deliberately destroyed.** `regrade_challenge` deletes every outcome of a challenge and puts every submission back to `queued`, so a fixed expected output can be applied without asking students to resubmit. Outcomes scored on *retired* test cases go too and never come back, since the judge only runs active ones — that is the difference between regrading and retiring, and it is why the confirmation dialog spells it out. Afterwards `count_testcase_outcomes` is zero for the challenge's test cases, so they are deletable again; nothing is left for a delete to strand. The route checks `count_running_submissions` first and refuses while a judge is mid-run, whose outcomes would otherwise land after the reset.
- **Language runtimes are pinned by `flake.lock`.** Changing a compiler version means updating the flake, not the system.
- **An isolate box is one flat directory.** `isolate_run` copies every path argument in under its *basename*, so unrelated paths collapse into one namespace, and the copy order — `box_files`, then `args`, then `additional_files` — decides who wins a collision. `box_files` is the explicit form and the only safe one when more than one file is involved: the evaluator gets its three arguments as `.actual_output`, `.expected_output` and `.test_input`, names nothing can collide with because `safe_filename` strips leading dots from every upload. Without it, a checker uploaded as `expected_output.c` compiles to a binary named `expected_output` that lands on top of the expected output it was meant to read.
- **Scores are per test case, in `[0, 1]`.** A submission's headline score is the mean of its outcomes' scores, shown out of 100. This aggregation assumes higher-is-better, so it is wrong for a challenge whose evaluator uses another scale — nothing enforces the range, and an evaluator is free to return any finite float.
- **The leaderboard's criterion is per challenge.** `challenge.ranking` is set by course staff in the editor and drives `query_challenge_leaderboard`, whose three variants live in `_LEADERBOARD_CRITERIA` as literal SQL keyed by the enum — the criterion is never taken from a request as text. `running_time` is the strict board: a submission is on it only if it passed *every* test, ranked by the sum of the per-test times. `total_score` and `average_score` rank by score, descending, and deliberately admit partially correct submissions, because a partial-credit evaluator is the case where nobody passes everything; both assume higher-is-better and so are wrong for an evaluator that is not, same as the headline score. Whichever the criterion, a student appears once, represented by their best submission, and every row carries all three metrics plus `n_tests` so the fragment can render the one that applies.
- **A block stops a person, it does not retract their work.** The judge grades a blocked student's queued submissions — `pull_queued_submission` does not know who submitted and should not learn: skipping those rows would strand them as `queued` forever, showing as pending in every teacher's standings and muddying the `count_running_submissions` check that guards a regrade. Standings and leaderboards go on listing them for the same reason: they are the record of what happened on a course, and it did happen. Nothing blocked is seeded either — a blocked `[dev] dummy_user` would lock a developer out of their own fresh checkout, which is exactly the trap "an administrator is never blocked" exists to avoid. **Removing an enrollment draws the same line**: `delete_enrollment` takes one row and nothing else, so the person loses access to the course and drops out of its standings, while every submission, outcome and leaderboard entry stays exactly where it was — `submission.user_id` has no `ON DELETE` tie to `enrollment`, and `query_challenge_standings`'s cohort is already a union with everyone who submitted. The confirmation on the people page says so in as many words.
- **A hidden test case gives away nothing but its verdict.** `visible_outcomes` in `main.py` blanks an outcome's `message`, `stdout` and `stderr` unless the test case is a public sample or the viewer is course staff: `stdout`/`stderr` are the submitted program's own output — the obvious way to print a hidden input back out — and `message` is now written by a *teacher's script that has just read the hidden input and the expected output*, which makes the rule matter more, not less — `diff.py` puts the expected output in it verbatim. The verdict, the score and the resource usage are always shown. Compiler output is exempt, and has its own block on the submission page: it does not depend on any test input. Redact in that function, not in the template, so the rule stays unit testable.
- **An anonymous htmx request gets `HX-Redirect`, not a 302.** `get_user` raises `NotAuthenticated` rather than returning a redirect — a dependency cannot return a `Response` — and the handler branches on the `HX-Request` header. This matters because most of the app is `f-*` fragments polled every few seconds: htmx follows a redirect inside the XHR, so a 302 would splice the whole login page into whatever div was polling, over and over. A 204 with `HX-Redirect` navigates the browser instead.
- **A secret has one source, and never a fixed default.** The source is the environment — `COCCO_SECRET_KEY`, `COCCO_GOOGLE_CLIENT_ID`, `COCCO_GOOGLE_CLIENT_SECRET` — and there is no file spelling for any of them, so there is no precedence to get wrong and nothing secret in a file that gets copied, diffed and committed by mistake. It is also what makes `cocco.dev.toml` committable at all. With `COCCO_SECRET_KEY` unset, `load_config` generates a random key per process and logs a warning: an unconfigured deployment then logs everybody out on every restart and no two workers agree, which is loud. A constant in the source tree would be a signing key that ships with the code, which is silent.
- **A `next` destination is filtered, never trusted.** `safe_next_path` keeps only a path on this site — one leading `/`, no scheme, no authority — so neither the `?next=` on the login page nor the one stashed in the session can be turned into an open redirect. It is applied at both ends: before the destination is stashed, and again after it is read back. The OAuth redirect URI deliberately carries no destination at all — Google matches that URI character for character against the console registration, so `next` travels in the session instead, and a callback arriving without one lands on the root.
- **CSRF rests on `same_site="lax"`, app-wide.** `POST /logout` is a form rather than a link so a planted `<img>` cannot fire it, and `lax` means the session cookie is not sent on a cross-site POST at all. That is the whole defence, and it is the same one every other mutating route in the app relies on — there is no CSRF token anywhere. A deliberate position, not an oversight: changing `same_site` would silently remove it.
- **Source is highlighted server-side.** `render_source` (the `highlight` Jinja filter) runs Pygments over a submission's source, using `submission.language` — every name in `languages.toml` happens to be a Pygments alias too — and falls back to plain text otherwise. The token colours are hand-written under `.cocco-code` in `assets/styles.css`, not a generated Pygments theme.
- **CSS is a build artifact.** Edit `assets/styles.css`, never `static/styles.css`. Tailwind only emits classes it can see, so it scans `templates/**/*.jinja` and `cocco/**/*.py` (`@source` directives); a class assembled dynamically at runtime will not be generated.
- **A static URL carries its own version, and that is what decides its `Cache-Control`.** `static_url()` stamps the file's mtime as `?v=`, and `VersionedStaticFiles` answers a URL that has one with a year of `immutable` and one that has not with `no-cache` — so a versioned URL can never serve a stale file (it changes when the file does) and a URL typed by hand can never be over-cached. The two halves only make sense together: bare `StaticFiles` sets no freshness directive at all, so every reload revalidated the navbar mark and the navbar painted an empty box until the 304 came back. That was the flicker. The mtime is read **per render, not at import**, because `npm run watch:css` rewrites `static/styles.css` without touching a `.py` and there would otherwise be nothing left to invalidate a year-long `max-age`; a missing file falls back to the bare path, since the stylesheet does not exist until the first `npm run build:css`. The mark is additionally **preloaded above the stylesheet** — the preload scanner promotes it instead of queueing it behind render-blocking CSS — and `base.jinja` binds `logo_url` once for both, because a preload whose href differs from the `src` by a single byte fetches the image twice.
