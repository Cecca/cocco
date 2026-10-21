---
status: draft
---

# General refactor: split `main.py` and `repository.py`

## Goal

The web interface lives mostly in two modules: `cocco/main.py` (8037 lines, about 190 routes and helpers) and
`cocco/repository.py` (4046 lines, about 200 queries). Both have grown past the point where a reader can hold the
module in their head, find the part they need without searching, or see what one area depends on.

Split them into **packages of modules, one per area of the app**, so that:

1. a change to the people page, say, touches `cocco/web/people.py` and `cocco/repository/courses.py`, and those
   files contain nothing else;
2. every module's imports say what it depends on (today every route can reach every query, because `main.py`
   imports all of them by name in one block);
3. no module is over about 1000 lines.

This is a **pure move**. No behaviour changes, no renames, no reformatting, no "while I'm here" fixes. Every
function, class and comment arrives in its new home byte for byte. Only import lines, decorators
(`@app.get` → `@router.get`) and the few file-relative paths listed below change.

## Where things stand (as of `65ec41c`)

`main.py` is, from top to bottom:

| Lines | What |
|---|---|
| 1–229 | imports, about 150 names from `cocco.repository` alone |
| 230–470 | `CONFIG`, the dummy-auth lists, `app` and its two middlewares, OAuth registration and `oauth_client`, `STATIC_DIR` / `TEMPLATES_DIR`, `VersionedStaticFiles` and the mount, `engine`, `templates`, `storage`, `_stored_file_download` / `_zip_stored`, `LANGUAGES`, the `MAX_*_BYTES` limits, `EDITOR_ROLES` |
| 470–620 | template filters and globals (`render_markdown`, `render_source`, `render_datetime*`, `render_score`, `render_duration`, `static_url`) |
| 622–1030 | `prepare_database` and the dummy seed data (`_populate_dummy_data`, ~350 lines) |
| 1032–1220 | `get_db`, `get_user`, `NotAuthenticated`, `Forbidden`, `Blocked` and their exception handlers |
| 1222–1855 | sign-in: login page, `_complete_sign_in`, OAuth providers, dummy auth, passwords, logout |
| 1856–2215 | the authorization dependencies (`require_admin`, `effective_*_role`, `require_course_*`, `require_challenge_*`), the API identity (`get_api_user`, `require_api_challenge_editor`), `authorize_submission`, `visible_outcomes` |
| 2216–2229 | the startup hook |
| 2230–2365 | home page, `/f/courses`, course creation |
| 2366–2588 | `/admin` |
| 2589–3085 | course page, enrolling, course editor, challenge reordering, course danger zone |
| 3086–3379 | people page |
| 3380–4075 | exercise sets: import, versions, previews, preview jobs, delete |
| 4076–4970 | exercise sessions, teacher side: gates, controls, pacing, dashboard, projector |
| 4971–5388 | exercise sessions, student side: strip, exercise page, Run/Check, jobs |
| 5389–5794 | challenge page, submissions, leaderboard, samples, submission page, standings, per-student pages, regrade |
| 5795–6208 | challenge creation and editor, challenge danger zone |
| 6209–6720 | evaluator, markdown preview, test cases |
| 6721–7211 | profile: identity, avatar, tokens, password |
| 7212–8037 | `/api`: challenge and exercise-set transfer |

`repository.py` already has rough sections: the shared rules (`_effective_archived_at`, `_challenge_visible`,
`_challenge_number`), the judge's side of the queue (sync, 134–444), enrollments, exercise sets (697), exercise
sessions (1225), the student's side of a session (1677), submissions and challenges (2018–2596), users, tokens
(2700), passwords (2779), test cases and challenge editing (2896–3279), archiving and deletion (3280–3568), then
the page queries for courses, users, profile and people (3569–4046).

Things that constrain the split:

- **`cocco.main:app` is the public entry point**: `pyproject.toml` (`entrypoint`), `server.py` (the import string
  uvicorn is handed), any deployment's systemd unit, `test_entrypoints.py`. It stays.
- **Route order is load-bearing** (CLAUDE.md, "Route order is load-bearing"). With routers, the order is the order
  of `include_router` calls, then the order inside each module.
- **Tests replace module attributes of `cocco.main`**: `main.CONFIG` (auth toggles), `main.DUMMY_AUTH`
  (`conftest.make_client`), `main.storage`, `main.oauth_client`, and three repository functions as looked up from
  `main` (`exercise_set_in_course`, `query_exercise_version`, `insert_session_participant`).
  `app.dependency_overrides[main.get_db]` keys on the function object. Once code moves, a patch on `main` no longer
  reaches it.
- **The judge uses `repository` as a namespace**: `from cocco import repository as repo`, then `repo.pull_queued_submission`
  and friends, and the tests patch `cj.repo.<name>` and `repo.requeue_exercise_job`.
- **Paths are resolved from `Path(__file__)`**: `STATIC_DIR` and `TEMPLATES_DIR` are `Path(__file__).resolve().parent / ...`.
  Moved one directory down, they would point into `cocco/web/`.
- **Rule files are loaded by path.** `.claude/rules/*.md` `paths:` frontmatter names `cocco/main.py` and
  `cocco/repository.py` (authorization, data-model, domain-rules, frontend, pages, storage). After the move those
  globs match nothing, so the guides silently stop loading.

## Design decisions

### Two packages, `cocco/web/` and `cocco/repository/`

- `cocco/web/` holds the HTTP layer as `APIRouter` modules. `cocco/main.py` stays, and shrinks to the **composition
  root** (about 100 lines): build `app`, add the two middlewares in their current order, mount `/static`, register
  the exception handlers, the startup hook, and `include_router` every router **in today's route order**.
  Exception handlers and `on_event` need the app, so they are registered in `main.py`
  (`app.add_exception_handler(Forbidden, handle_forbidden)`) while the handler functions live with their exceptions.
- `cocco/repository.py` becomes `cocco/repository/`, a package of modules. Its `__init__.py` is the current module
  docstring and **nothing else: no re-exports**. Callers import from the submodule that owns the query
  (`from cocco.repository.challenges import query_challenge`). A facade would be a 200-name list to keep in step, would
  hide which area a route depends on (the thing this refactor is for), and would make patching the facade a
  silent no-op for intra-package calls.
- The rest of `cocco/` stays flat. `cocco/auth.py`, `tokens.py`, `passwords.py`, `storage.py`, `transfer.py` are
  already the right size and are shared with the CLI or the judge.

### `cocco/web/` modules

| Module | Takes (old `main.py` lines) | ~Lines |
|---|---|---|
| `core.py` | `CONFIG`, `DUMMY_AUTH`, `engine`, `storage`, `_stored_file_download`, `_zip_stored`, `LANGUAGES`, `EXTENSION_TO_LANGUAGE`, `EDITOR_ROLES`, `EDITOR_ROLE_NAMES`, `get_db`, `DBDep`; any `MAX_*` limit used by more than one module | 200 |
| `templating.py` | `STATIC_DIR`, `TEMPLATES_DIR`, `VersionedStaticFiles`, `templates`, every filter and global, `static_url` | 200 |
| `seed.py` | `DUMMY_ADMIN` / `DUMMY_TEACHERS` / `DUMMY_STAFF` / `DUMMY_STUDENTS`, `prepare_database`, `_seed_user`, `_populate_dummy_data` | 430 |
| `access.py` | `NotAuthenticated`, `get_user`, `CurrentUser`, `_return_path`, `Forbidden`, `Blocked`, their handlers, `require_admin` … `ChallengeEditor`, `ApiNotAuthenticated`, `get_api_user`, `ApiUser`, `require_api_challenge_editor`, `authorize_submission` | 550 |
| `signin.py` | 1222–1855: OAuth registration, `oauth_client`, login page, `_complete_sign_in`, providers, dummy, password routes, logout | 650 |
| `home.py` | `index`, `_render_courses`, `/f/courses` | 80 |
| `admin.py` | 2366–2588 | 220 |
| `courses.py` | `/courses/new` (both), 2589–3085 | 560 |
| `people.py` | 3086–3379 | 300 |
| `exercise_sets.py` | 3380–4075, plus `MAX_EXERCISE_FILE_BYTES`, `MAX_EXERCISE_CODE_BYTES` | 700 |
| `exercise_sessions.py` | 4076–4970 | 900 |
| `exercise_student.py` | 4971–5388 | 420 |
| `challenges.py` | 5389–5794, plus `visible_outcomes` and `_OUTCOME_FREE_TEXT` | 430 |
| `challenge_editor.py` | 5795–6208 | 410 |
| `testcases.py` | 6209–6720 (evaluator, `post_markdown_preview`, test cases), plus `MAX_TESTCASE_BYTES`, `MAX_EVALUATOR_BYTES` | 510 |
| `profile.py` | 6721–7211 | 490 |
| `api.py` | 7212–8037, plus `MAX_TRANSFER_BYTES` | 830 |

The three exercise modules match the existing test files (`test_exercise_routes.py`, `test_exercise_sessions.py` +
`test_exercise_dashboard.py`, `test_exercise_student.py`), as do `admin`, `people`, `profile`, `testcases`, `api`.

Placement rules for the borderline cases:

- **A dependency specific to one area stays with that area**: `require_exercise_set_editor` in `exercise_sets.py`,
  `require_exercise_session_*` / `require_session_exercise` / `ExerciseLocked` in `exercise_sessions.py`,
  `require_profile_*` in `profile.py`, `require_api_exercise_set_editor` in `api.py`. `access.py` holds only the
  identity and the course/challenge gates every area shares, so `authorization.md`'s "exactly one place" for the
  admin promotion (`effective_course_role`) stays one place.
- **A helper used by two modules lives in the one that defines the concept, and the other imports it.** The danger
  zone helpers (`_danger_alert`, `_judge_busy_message`, `_confirmation_matches`) stay in `courses.py`;
  `challenge_editor.py` imports them. `exercise_check_rows` and the job helpers stay in `exercise_sets.py`;
  `exercise_student.py` imports them.
- **Names keep their leading underscore.** Inside a package, siblings may import each other's `_names`
  (package-private); nothing outside `cocco.web` / `cocco.repository` may. Renaming would double the diff and
  break `git log -S`.
- **Imports form a DAG**: `core`, `templating` ← `access` ← area modules ← `main`. An area module may import from
  another area module only along the one-way edges listed above (`courses` → `challenge_editor`, `exercise_sets` →
  `exercise_sessions` → `exercise_student`). Nothing under `web/` imports `cocco.main`.

### `cocco/repository/` modules

| Module | Takes | ~Lines |
|---|---|---|
| `rules.py` | `_effective_archived_at`, `_challenge_visible`, `_challenge_number`, `_EMPTY_JSON_ARRAY`, `_json_object`: the rules `domain-rules.md` says are spelled once | 130 |
| `judging.py` | the queue: `QUEUE_CHANNEL`, `notify_queue`, and the judge's sync side (`pull_queued_submission` … `prune_run_jobs`, `RUN_JOB_RETENTION`) | 320 |
| `users.py` | `insert_user`, `_USER_COLUMNS`, `query_user*`, `update_user`, `record_sign_in`, `block_user` / `unblock_user`, `is_administrator`, `insert_administrators`, `query_users`, `query_user_profile` | 450 |
| `credentials.py` | API tokens (2700–2778) and passwords (2779–2895) | 200 |
| `courses.py` | enrollments (`insert_enrollment` … `course_exists`), `query_user_role`, `query_course_role`, `insert_course` / `update_course`, `query_courses`, `query_course_people`, `query_course`, `query_enrollment_key`, `_course_challenges`, `_listed_challenges`, `query_course_info`, `query_course_challenges`, `query_ordered_challenge_ids`, `reorder_challenges` | 650 |
| `challenges.py` | `insert_submission`, submissions and outcomes (2018–2208), `query_challenge` … `query_sample_testcase`, `challenge_in_course`, test cases and challenge editing (2896–3126, 3200–3279), regrading | 900 |
| `exercise_sets.py` | 697–1169, `insert_exercise_job` | 540 |
| `exercise_sessions.py` | 1225–2017: sessions, pacing, the student's side and the session page queries | 800 |
| `lifecycle.py` | archiving and every hand-written cascade: 3280–3568, plus `delete_exercise_set` / `delete_exercise_session` (1170–1224) | 350 |

`lifecycle.py` gathers the whole deletion cascade (`domain-rules.md`, "Deleting a challenge") in one module:
`delete_course` calls `delete_challenge`, `delete_exercise_set` and `delete_exercise_session`, and the lock order
(`submission` before `testoutcome`) is easier to check when the locks and the deletes sit together. It is also what
keeps the module graph acyclic: `rules` ← everything; `lifecycle` imports nobody but `rules`.

The judge switches to `from cocco.repository import judging as repo`, so its `repo.<name>` call sites and the tests'
`cj.repo.<name>` patches are unchanged. `test_exercise_judge.py`'s `monkeypatch.setattr(repo, "requeue_exercise_job", ...)`
retargets `judging`.

### Seams tests patch: read through the module, never imported by name

`CONFIG`, `DUMMY_AUTH` and `storage` live in `web/core.py`; `oauth_client` lives in `web/signin.py`. Every module
that uses one of these **imports the module and reads the attribute** (`from cocco.web import core` …
`core.CONFIG.auth.password_login`, `core.storage.resolve(...)`), so one `monkeypatch.setattr(core, "CONFIG", ...)`
reaches every route. These are the only call sites that change spelling, and they are few: `CONFIG` is read in
sign-in, admin, people and profile; `storage` in challenges, test cases, evaluator and the API; `DUMMY_AUTH` in
sign-in. Everything else (`templates`, `LANGUAGES`, limits, roles) is imported by name.

`cocco/main.py` **re-exports nothing** for tests. A test still patching `main.CONFIG` would then be patching a
name that doesn't exist (`monkeypatch.setattr` raises on a missing attribute), instead of passing while testing
nothing.

The three tests that patch a repository function as seen from `main` patch it on the route module that calls it
(`cocco.web.exercise_sets.exercise_set_in_course`, `cocco.web.exercise_sets.query_exercise_version`,
`cocco.web.exercise_student.insert_session_participant`).

### File-relative paths

`STATIC_DIR` and `TEMPLATES_DIR` become `Path(__file__).resolve().parent.parent / ...`, the one non-import line
change in moved code. `test_main.py` (or a new check) asserts both directories exist and contain `base.jinja` /
`styles.css`'s directory, since a wrong path only shows at the first render.

### Logger names

`logging.getLogger(__name__)` changes `cocco.main` to `cocco.web.<module>` in JSON logs' `logger` field. Nothing in
the code or the example configs filters on `cocco.main` (only `logs.py`'s docstring mentions it); the access log is
`cocco.access`, unchanged. Note it in the commit message, since a deployment's log query may name the old logger.

## Steps

Each step is one commit, and `uv run pytest` passes at every commit (`-m "not integration"` for the web steps; the
whole suite, integration included, for steps 1 and 2, which touch the judge's imports).

0. [ ] **Guards.** Two checks, both committed, both passing on today's code:
   - **Route shadowing test** (permanent, in `test_main.py`): for every route in `app.routes`, build a concrete URL
     by substituting `1` for each path parameter, and assert that the *first* route matching that method and path
     is the route itself. That is the general form of "`/courses/new` above `get_course`, every two-segment course
     page above `get_challenge`", and it keeps holding after routers change who declares what.
   - **Move check** (throwaway script in the scratchpad, not committed): parse `main.py` and `repository.py` at
     `65ec41c` and the new packages at `HEAD` with `ast`, and assert that every top-level definition exists exactly
     once after the step, with identical `ast.dump` apart from decorators and the attribute reads listed under
     "Seams". Run it after every step. Also dump `[(r.methods, r.path, r.name) for r in app.routes]` at
     `65ec41c` and diff it against each step's: the list must be identical, order included.
1. [ ] **`repository` becomes a package.** `git mv cocco/repository.py cocco/repository/__init__.py` would keep
   history but leave a facade, so instead: create the package with `rules.py` and `judging.py` first, move those
   definitions, keep everything else in a temporary `cocco/repository/_rest.py`, and point every importer at the
   right module. Judge to `judging as repo`. Run the integration tests.
2. [ ] **Carve `_rest.py`** into `users.py`, `credentials.py`, `courses.py`, `challenges.py`, `exercise_sets.py`,
   `exercise_sessions.py`, `lifecycle.py`, one commit per two or three modules, each updating `main.py`'s,
   `auth.py`'s and the tests' imports. `_rest.py` is gone by the end. `test_repository.py` imports from the new
   modules; consider splitting it to match (`test_repository_courses.py`, …) only if it reads better, as a
   separate commit.
3. [ ] **`web/core.py`, `web/templating.py`, `web/seed.py`.** Move the module-level state, filters and seed data.
   `main.py` imports them. Switch every reader of `CONFIG`, `DUMMY_AUTH`, `storage` to `core.<name>`, and the tests
   and `conftest.make_client` to patch `core` (`app.dependency_overrides[core.get_db]`). Fix the two `Path(__file__)`s.
4. [ ] **`web/access.py`.** Move the identity, refusals and gates. `main.py` registers the three exception handlers
   with `add_exception_handler`. `test_auth_routes.py`'s `from cocco.main import Forbidden, require_...` moves too.
5. [ ] **Routers, one area per commit**, in route order so each commit's `include_router` lands after the previous
   one and before what is still in `main.py`: `signin`, `home`, `admin`, `courses`, `people`, `exercise_sets`,
   `exercise_sessions`, `exercise_student`, `challenges`, `challenge_editor`, `testcases`, `profile`, `api`. Each
   commit retargets the tests that reach into that area (`main.session_job_refusal`, `main.projector_view`, …,
   mostly in `test_exercise_dashboard.py` and `test_exercise_student.py`). The route-table diff must stay empty.
   `/courses/new` goes at the top of `courses.py` with the route-order comment it has today; the comment explaining
   why two-segment course pages come before `get_challenge` moves to `main.py`, next to the `include_router` calls
   that now enforce it.
6. [ ] **`main.py` is the composition root.** What is left: `app`, middlewares, mount, handlers, startup, routers.
   Remove the imports nothing uses. Extend `test_entrypoints.py` so `server.py` importing `cocco.web.core` fails
   as importing `cocco.main` does (it reads the config at import too).
7. [ ] **Documentation.**
   - `.claude/rules/*.md` `paths:` frontmatter: replace `cocco/main.py` / `cocco/repository.py` with the modules each
     guide is about (e.g. `pages.md` → `cocco/web/**`, `cocco/repository/**`; `authorization.md` →
     `cocco/web/access.py`, `cocco/web/people.py`, `cocco/web/profile.py`, …; `storage.md` → the modules reading
     `core.storage`; `frontend.md` → `cocco/web/templating.py`). Check each guide's body for "in `main.py`".
   - `CLAUDE.md`: "Queries (`cocco/repository.py`)" and "HTTP layer (`cocco/main.py`)" describe the packages and the
     module map in a few lines; "Route order is load-bearing" names `include_router` order and the shadowing test;
     add the seam rule ("`CONFIG`, `DUMMY_AUTH`, `storage`, `oauth_client` are read through their module").
   - `.claude/agents/implementer.md`: "Queries go in `cocco/repository.py`" → the package.
   - Comments and docstrings naming `repository.py` / `main.py` (`model.py`, `passwords.py`, `tokens.py`,
     `logs.py`, `conftest.py`, `server.py`). Leave migration revisions alone: they are history.
   - Set this plan's status to `done` with an outcome section.

## Verification

- The route table at `HEAD` equals the one at `65ec41c`, order included; the shadowing test passes.
- The move check passes after every step: nothing lost, nothing duplicated, nothing edited beyond the listed lines.
- `uv run pytest` (integration included) passes.
- `git diff --color-moved=dimmed-zebra 65ec41c` shows almost everything as moved; the non-moved lines are imports,
  decorators, the seam reads and the two paths.
- `wc -l cocco/web/*.py cocco/repository/*.py`: nothing over ~1000 lines, `main.py` around 100.
- `just web` + `just judge`: sign in with dummy auth, open a challenge, submit, watch it grade; open an exercise
  session as teacher and student. `just build` and check the wheel contains `cocco/web/` and `cocco/repository/`
  (`packages = ["cocco"]` should include subpackages; confirm).

## Risks

- **A patch that silently stops patching.** Retargeted tests that still pass may be passing for another reason.
  `test_exercise_routes.py`'s `exercise_set_in_course` patch is the worst case: without it the route also answers
  404, so the test cannot tell. For each of the three repository-function patches, break the patch on purpose once
  (patch in a function that raises) and confirm the test fails.
- **Route order.** Covered by the route-table diff during the refactor and the shadowing test afterwards. The
  shadowing test only tries `1` for each parameter, which is what today's collisions need; it won't catch a
  collision that depends on a parameter's value.
- **Import cycles** show up as `ImportError` at collection, so they are loud, but the fix must respect the DAG
  above rather than a function-level import.
- **Import-time side effects move.** OAuth registration, template globals and `create_async_engine` happen when
  their module is imported. `main.py` must import everything the app needs before serving, which `include_router`
  guarantees for route modules; `templating.py` registers globals at its own import, before any route renders.
- **Conflicts with work in flight.** Any open branch touching `main.py` or `repository.py` will conflict on every
  hunk. Land or rebase other work first, and do the whole refactor in one short sitting rather than over weeks.
- **Rule files that stop loading.** Covered in step 7; until then the guides don't load for the new files. Doing
  step 7's frontmatter part together with step 1 would avoid that window.

## Not in scope

- Switching `@app.on_event("startup")` to a lifespan handler.
- Renaming anything, or changing a helper's signature.
- Splitting `judge.py` (2213 lines) or `cli.py` (1059 lines). Worth a plan of their own.
- Moving tests into a `tests/` directory, or renaming `test_main.py`.

## Rejected

- **A facade `cocco/repository/__init__.py` re-exporting everything**, so no caller changes. It keeps the import
  list this refactor wants to get rid of, hides which area depends on which queries, and makes a patch on the
  package miss calls between its own modules.
- **`main.py` re-exporting moved names**, so tests keep `main.X`. A patch on a re-export reaches nothing and the
  test passes anyway.
- **Flat modules** (`cocco/routes_admin.py`, `cocco/queries_courses.py`, …). Two dozen more files beside the 50
  already in `cocco/`, with the grouping carried by a name prefix instead of a directory.
- **Dependency-injected state** (`request.app.state.storage`, a `Depends(get_storage)`) instead of module seams.
  Cleaner for tests in the long run, but it changes every route's signature, which is a behaviour-adjacent change
  this pure move should not carry. A later plan can do it on top.
- **One commit for everything.** Reviewable only through `--color-moved`, and a failure can't be bisected to an
  area.
