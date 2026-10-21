---
status: done
---

# Replace the SQL string fragments in `repository.py` with Core

`cocco/repository.py` builds several page queries by splicing module-level strings into `text()`:
`_EFFECTIVE_ARCHIVED_SQL`, `_CHALLENGE_VISIBLE_SQL`, `_COURSE_CHALLENGES_CTE`, `_VISIBLE_TO_CALLER_SQL`,
`_EXERCISE_VERSIONS_CTE`, `_EXERCISE_SET_TITLE_SQL`, `_EXERCISE_TITLES_SQL`, `_SESSION_COHORT_CTE`,
`_SESSION_REVEALED_SQL`, `_SUBMISSION_OUTCOMES_CTE` (and `_session_checks_cte()`, the same thing as a function).

The goal is to remove all of them, without giving up the one-JSON-document-per-page shape that `DESIGN.md`
("Database design") asks for.

## Why

Each fragment depends on something it doesn't name, and nothing checks that the two sides agree:

- **Aliases of the caller's query.** `_EXERCISE_SET_TITLE_SQL` needs aliases `current` and `latest`, `_EXERCISE_TITLES_SQL` a
  row `v`, `_SESSION_REVEALED_SQL` a row `s`, and `_EFFECTIVE_ARCHIVED_SQL` both `challenge` and `course` in the FROM.
- **Other CTEs.** `_SESSION_COHORT_CTE` reads `checks`, which only exists because `_session_checks_cte()` was pasted
  just above it.
- **Bound parameters.** `_COURSE_CHALLENGES_CTE` needs `:course_id` and `:user_id`, `_SUBMISSION_OUTCOMES_CTE` needs
  `:submission_id`, and `_VISIBLE_TO_CALLER_SQL` turns a Python bool into the SQL parameter `:include_hidden`.
- **Text templating.** `_EXERCISE_VERSIONS_CTE` gets its WHERE clause through `.format(where=...)`.
- **Rules written twice.** `_EFFECTIVE_ARCHIVED_SQL` / `_effective_archived_at()` and `_CHALLENGE_VISIBLE_SQL` /
  `_challenge_visible()` each spell one rule twice, kept in step only by tests (`test_repository.py`, the two
  tests whose docstrings name both spellings; `test_danger_zone_routes.py`, the one naming `_EFFECTIVE_ARCHIVED_SQL`).

## Approach

Rewrite the queries that use a fragment in SQLAlchemy Core. Each fragment becomes a small function that returns a
Core object, and whatever it depended on becomes an argument:

| Fragment | Replacement |
|---|---|
| `_EFFECTIVE_ARCHIVED_SQL` | deleted; `_effective_archived_at()` is the only definition |
| `_CHALLENGE_VISIBLE_SQL` | deleted; `_challenge_visible()` is the only definition |
| `_COURSE_CHALLENGES_CTE` | `_course_challenges(course_id, user_id) -> CTE` |
| `_VISIBLE_TO_CALLER_SQL` | `if not include_hidden: stmt = stmt.where(challenges.c.visible_from.is_not(None))` in the caller |
| `_EXERCISE_VERSIONS_CTE` | `_exercise_versions(condition) -> CTE`, `condition` a Core boolean expression |
| `_EXERCISE_SET_TITLE_SQL` | `_exercise_set_title(current, latest)` |
| `_EXERCISE_TITLES_SQL` | `_exercise_titles(document)`, a scalar subquery over the column passed in |
| `_SESSION_REVEALED_SQL` | `_session_revealed(session_id)`, a scalar subquery (`query_session_heading` already builds the `array_agg` form in Core) |
| `_session_checks_cte(one_user)` | `_session_checks(session_id, user_id=None) -> CTE` |
| `_SESSION_COHORT_CTE` | `_session_cohort(checks, session_id) -> CTE` |
| `_SUBMISSION_OUTCOMES_CTE` | `_submission_outcomes(submission_id) -> agg` (one-row CTE) |

Core equivalents for the Postgres constructs these queries use:

| SQL | Core |
|---|---|
| `json_agg(x ORDER BY …)` | `func.json_agg(aggregate_order_by(x, …))` (`sqlalchemy.dialects.postgresql`) |
| `row_to_json(cte)`, `json_agg(cte)` of a whole row | `cte.table_valued()` |
| `count(*) FILTER (WHERE …)` | `func.count().filter(…)` |
| `JOIN LATERAL (…)` | `subquery.lateral()` |
| `jsonb_array_elements(…) WITH ORDINALITY AS x(e, i)` | `func.jsonb_array_elements(…).table_valued("value", with_ordinality="i")` |
| `row_number() OVER (…)` | `func.row_number().over(partition_by=…, order_by=…)` |
| `coalesce(…, '[]'::json)` | `func.coalesce(…, literal_column("'[]'::json"))` |

Queries that use no fragment (the leaderboard, standings, and the rest) stay as they are. The leaderboard's
`_LEADERBOARD_CRITERIA` is enum-keyed literal SQL by design and is out of scope.

## Other cleanups along the way

Not fragments, but the same work makes them easy:

- **Score aggregate.** `avg(coalesce(score, 0))`, `sum(elapsed_s)` and the `n_passed` / `n_tests` counts are written
  three times (`_SUBMISSION_OUTCOMES_CTE` and two queries around it). A `_score_columns(outcomes)` helper can cover
  all three.
- **Version numbering.** The per-set `row_number() OVER (PARTITION BY v.set_id ORDER BY v.id)` is written three
  times (`_EXERCISE_VERSIONS_CTE`, `query_exercise_version_summary`, `query_exercise_session`).

## Steps

Each step is one commit, and the suite passes at every commit.

0. [x] **Snapshot harness.** Add a throwaway test, kept out of the commit history or deleted at the end, that seeds the
   database, calls each affected query, and compares the JSON with a snapshot taken before the change. Templates
   read keys by name, so a renamed key, a missing key or a duplicate one (the `effective_archived_at` /
   `archived_at` clash) breaks a page even when the tests pass. Compare parsed JSON, not text: key order may change.
1. [x] **Submission outcomes.** `query_submission`, `query_submission_outcomes`. This step settles the Core style
   (`json_agg`, `row_to_json`, whole-row CTEs) on a small, well-tested pair, and adds `_score_columns`.
2. [x] **Challenges.** `query_challenge`, then `_course_challenges` with `query_course_info` and `query_course_challenges`.
   Delete `_EFFECTIVE_ARCHIVED_SQL`, `_CHALLENGE_VISIBLE_SQL` and `_VISIBLE_TO_CALLER_SQL`. `number` must still be
   ranked inside the CTE over the whole course, before the visibility filter, so a student sees the numbers staff
   see. Change the pinning tests to test behaviour only.
3. [x] **Exercise sets.** `_exercise_versions`, `_exercise_set_title`; `query_exercise_sets`, `query_exercise_set`,
   then `query_exercise_version_summary` for the shared numbering.
4. [x] **Exercise sessions.** `_exercise_titles`, `_session_revealed`, `_session_checks`, `_session_cohort`;
   `query_exercise_session`, `query_session_strip`, `query_session_progress`, `query_session_counts`. The last three
   are polled by a whole class, so compare `EXPLAIN ANALYZE` before and after on a seeded session.
5. [x] **Documentation.** In CLAUDE.md, rewrite the conventions that name the fragments: "Archived" and "Visible"
   are no longer "spelled exactly twice", and "The number on a challenge card" names `_COURSE_CHALLENGES_CTE`.
   Update the docstrings and comments that refer to removed names. Remove the snapshot harness.

## Verification

- `uv run pytest -m "not integration"` after every step, and the snapshot comparison for the queries touched.
- After the last step, `grep -nE '_[A-Z_]+_(CTE|SQL)\b' cocco/` finds nothing.
- Visual check with `just web` of the course page (student and staff), a challenge, a submission while it's being
  graded, an exercise set, and a session's dashboard and projector.

## Risks

- **Readability.** The long page queries get longer and harder to read than the SQL beside them. Where one clearly
  reads worse, a middle ground is to keep it as `text()` with Core-compiled pieces passed in. Use that only as an
  exception, and note it in the code.
- **Different SQL.** Core may generate SQL that the planner treats differently, e.g. a scalar subquery where a
  lateral join was. That matters only for the polled queries, which are covered by step 4's `EXPLAIN ANALYZE`.
- **Overlap with `2026-10-18-general-refactor.md`**, which splits `repository.py` into modules. Doing this first makes
  that split easier: the helpers above go with the queries that use them, instead of module-level strings shared
  across the file.

## Rejected

- **Inline every fragment.** This loses the single definition of the archived and visible rules, which is why
  they were extracted.
- **Postgres views or SQL functions** (e.g. a challenge view with `effective_archived_at`, `is_visible`, `number`).
  These also give a single definition, but each change needs a hand-written migration (autogenerate can't see
  views; see `.claude/rules/migrations.md`). Postgres also expands `challenge.*` when the view is created, so a
  column added to the table later silently never appears in the view.

## Outcome (2026-10-21)

Done on branch `sql-fragments-to-core`, one commit per step (step 0 has none: the harness was never committed). Each
step was written by an implementation agent and approved by a separate review agent before it was committed.

- **Step 0.** An untracked snapshot test seeded every case the fragments distinguish (archived course and challenge,
  hidden / scheduled / visible challenges, running and queued submissions with NULL scores and retired cases,
  several exercise set versions, a session with sticky solves, error results and an unregistered checker) and
  recorded 24 queries across all viewer and parameter combinations. It loaded JSON through a decoder that rejects
  duplicate keys. Every later step compared clean against the frozen baseline, which was never re-recorded.
- **Step 1** (`de95eda`). `_submission_outcomes()` (a one-row CTE), `_score_columns()`, `_EMPTY_JSON_ARRAY`.
- **Step 2** (`998fa47`). `_course_challenges()`, `_listed_challenges()` (the `include_hidden` filter, outside the CTE
  so `number` is still ranked over the whole course), `_challenge_number()` shared with `query_challenge`. The tests
  that pinned two spellings now test behaviour; a mutation run confirmed they fail if either rule is broken.
  `query_challenge`'s samples became a filtered subquery instead of a site-wide grouped CTE.
- **Step 3** (`ae18080`). `_exercise_versions(condition, *, counts=True)`, `_exercise_set_title()`,
  `_version_number()` (now the only version numbering, also in `query_exercise_version_source`),
  `_count_problems()`, `_latest_version()`. `query_editable_exercise_sets` moved to Core as well, because it used the
  title fragment. Core now types its `latest_status` as the enum, so `api_list_exercise_sets` serialises `.value`.
- **Step 4** (`7e7af82`). `_exercise_titles()`, `_session_revealed()` (now also used by `query_session_heading`),
  `_session_checks()`, `_session_cohort()`, plus `_session_document()`, `_checks_of()`, `_json_object()`.
  `kind = 'check'` is rendered as a literal everywhere (`_is_check()`). Under psycopg's automatic prepared statements
  this keeps `ix_exercise_job_check`'s partial-index predicate provable, which was verified with a forced generic
  plan. `EXPLAIN (ANALYZE, BUFFERS)` on a 90-person, 15-exercise session with about 23k jobs showed the same plans and
  timings before and after (progress 13.1 → 11.9 ms, counts 4.9 → 4.9 ms, strip 0.2 ms). Reveals went from a
  correlated SubPlan to a one-shot InitPlan.
- **Step 5.** CLAUDE.md, `.claude/rules/exercises.md`, the `conftest.py` docstring and comments in `model.py`,
  `main.py` and three templates no longer name removed fragments. The harness is deleted.

Verification: the non-integration suite passes at every commit (965). `grep -nE '_[A-Z_]+_(CTE|SQL)\b' cocco/` finds
nothing. The visual check with `just web` was **not** done; the suite renders every page involved, but a look at the
course page, a challenge, a grading submission, an exercise set and a session's dashboard and projector is still
worth doing before merge.

No `text()` exception was needed. Queries that used no fragment stay `text()`, as planned.

Deferred, deliberately out of scope:

- `query_user_submissions` and `query_challenge_standings` still spell `_score_columns`' aggregates in `text()` (a
  "keep in step" note sits on `_score_columns`). Using the helper means rewriting both in Core.
- `query_exercise_sessions` still splices one of two literal `WHERE` strings into `text()`, and re-spells
  `_n_exercises` as `jsonb_array_length(...)`.
- `query_exercise_session`'s `max_checked` and `query_max_checked_exercise` compute the same thing.
- `prune_run_jobs` compares `kind` with a bound parameter. That is harmless, because its `OR` can't use the partial
  index anyway.
- `query_session_progress` scans the whole `checks` CTE once per person (about 7 of its 12 ms on the seed above). This
  predates the change; aggregating per user once and joining would remove it.
- `_latest_version()` calls `.correlate(ExerciseSet)`, but SQLAlchemy doesn't enforce it. Used without
  `exercise_set` in the outer FROM, it would silently pick the newest version site-wide.
