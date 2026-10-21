---
paths:
  - "cocco/exercises/**"
  - "cocco/templates/*exercise*"
  - "cocco/static/exercise.js"
  - "cocco/test_exercise_*.py"
---

# Exercises (`cocco/exercises/`, the `exercise_*` tables)

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
grid alike. Both read the check aggregate `_session_checks(session_id, user_id=…)`, so the two screens
cannot disagree. The cohort is `_session_cohort`: participants, unioned with anyone who
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
