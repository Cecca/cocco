---
paths:
  - "cocco/exercises/**"
  - "cocco/templates/*exercise*"
  - "cocco/static/exercise.js"
  - "cocco/test_exercise_*.py"
---

# Exercises (`cocco/exercises/`, the `exercise_*` tables)

Short programming exercises (Python, C, any language in `languages.toml`), wooclap-style: a teacher runs a set live in class, sets the pace, and
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
lines, and an optional `language:` (a key of `languages.toml`, default `python`; anything else is
a parse error listing them). A code block may be prefixed by the language's name or any of its
`extensions` (`python` / `py`, `c`, `cpp` / `cc` / `cxx`), and a block prefixed with *another*
language's spelling is an error naming the right one, for `_ITALIAN`'s reason (`linguaggio` is
there too). An `error` block is one word. Its module docstring is the reference. The prototype's Italian keywords are **errors that
name the English word, not aliases** (`_ITALIAN`). A ` ```soluzione ` block left unconverted would
otherwise be "any other code block", which stays part of the statement and would put the solution
on every student's screen. Parsing is synchronous, at import: a file that does not parse is a 200
with its problems and creates no version.
`compare.py` holds the comparison rules (`exact`, `spaces`, `case`, `numbers <tol>`, `order`).
`hints.py` holds the one-line English explanation of each failure name (`HINTS`: Python exceptions
and signals like `SIGSEGV`). Like `transfer.py`,
`format.py`, `compare.py` and `hints.py` touch no database, FastAPI or judge, and are tested by
import.

**There is no harness: a program runs as `Language.run` runs it** (`python main.py`, or the compiled
binary), through the challenge judge's two calls and no per-language code. `prepare_exercise_program`
writes the source as `main.<extensions[0]>` (the name diagnostics and tracebacks report) in a private
`workdir` and calls `Language.compile` (a no-op when interpreted, a box compile with the `COMPILE_*`
budget otherwise, so `#include "/etc/passwd"` is "no such file"). `run_exercise_program` calls
`Language.run` on one stdin, with `exercise.time_s`, `EXERCISE_MEM_LIMIT_KB` and
`EXERCISE_FSIZE_KB` (256 KB), and `read_exercise_run` turns the `RunOutcome` into an
`ExerciseExecution`. A check prepares **once** and runs every test on the result (one compile, N
runs); a build prepares the solution once for both its rounds, and the starter once, each in its own
subdirectory of the job's `workdir`. What differs between languages is data in `languages.toml`,
never a branch: `env` (python's `PYTHONHASHSEED=0`, so a `set` prints alike everywhere),
`error_name`, `editor_mode`.

**`read_exercise_run`'s order matters.** `XX` raises (the judge's error); `TO` is `timeout`; `oom`
is `memory`; then **truncated**: `SIGXFSZ`, or stdout or stderr at the 256 KB cap (compared in
bytes). It is checked *before* `RE` because Python ignores `SIGXFSZ` and exits 120 with an `OSError`
instead, and that must not read as `error = OSError`; the cost is that a program writing exactly
256 KB is flagged too. Then `OK` (exit 0), `RE` (exit = isolate's `exitcode`, `error` from
`error_name`) and `SG` (exit = 128 + signal, `error` = the signal's name, `SIGSEGV`). The exit code
and signal come from the meta file (`RunOutcome.exitcode` / `exitsig`), because `retcode` is
isolate's own. `no_report` is no longer produced; the literal stays so stored results validate.
stdout and stderr go through `_jsonb_safe` (strips NULs and lone surrogates), the one boundary, so
what is compared and what is stored are the same bytes. Output is lost when a program dies by a
signal (stdout is a file, so fully buffered); stderr keeps what was unbuffered.

**The error name** is how `error:` blocks, hints and hidden tests name a failure. It is a signal's
name, which is the operating system's and the same for every language, or else the language's
optional `error_name` regex searched in stderr when the program exited non-zero: last match wins,
group 1. Python's anchors on an **indented** line followed by a column-0 name
(`(?m)^[ \t]+.*\n([A-Za-z_][\w.]*)(?::|$)`): every traceback frame's source, caret or `File` line is
indented, a plain stderr line (`sys.exit('bye')`) and the first line of a message are not, so a
multi-line message or a printed word is not mistaken for the exception. It is linear; the obvious
`^(?:[ \t]+.*\n)+` form is quadratic. A language without the key has failures with no name
(`error = None`, as for `sys.exit(1)` / `return 1`). stderr is the program's to forge, so a program
can fake the error check; nothing is graded. An `error` block passes on an **exact match** of that
name: no subclasses (there is no MRO), so `error: LookupError` does not pass on an `IndexError`.
`KeyboardInterrupt` is `SIGINT` and an `ExceptionGroup` has no name; both accepted.

**No prompts.** `input("n? ")` writes `n? ` to stdout like any print, and everything printed is
compared, so students are told not to pass a prompt (the format docstring and the exercise page
say so). The Run tab shows the stdin given above the transcript, for every language.

**A compile is a result of its own.** `ExerciseCheckResult` and `ExerciseRunResult` carry
`compile: ExerciseCompile | None` (`ok`, `diagnostics` capped at `MAX_EXERCISE_STORED_OUTPUT`), set
whenever the language has a compiler. A failed compile is `passed=False, tests=[]` (so *attempted*)
or an empty run; warnings on a successful one are shown collapsed. Diagnostics concern the student's
own source only, so they are shown in full even where hidden tests are redacted.

**Old results are folded, not migrated.** `ExerciseExecution`'s before-validator
(`_fold_old_exception`) turns a stored `exception` object into `error` (its `type`) plus its
traceback appended to `stderr`; a result without `compile` validates as it is. Versions built by the
harness stay as they were: their expected outputs were computed with prompts dropped and their
`error` blocks may name a base class, so a teacher re-imports (a new version) and switches to get
the new semantics.

**A session's language is fixed.** Switching to a version in another language is refused (every
draft and "solved" was in one language); a *set* may change language between imports.

**What a new language needs**: a `languages.toml` entry with `editor_mode` (and optionally
`error_name`), a vendored CodeMirror mode if `clike.js` does not cover it (`_exercise-editor-head`
loads every vendored mode), and `HINTS` lines for its common errors. Java would also need a
`Main` source stem (`java Main`), which `main.<ext>` does not give.

**Comparison happens in the judge process**, not in an evaluator box: the rules are declarative
and the code applying them is ours, and a box per test would double a check's cost to buy nothing.
It goes through `asyncio.to_thread`, because `order` with `numbers` is quadratic in the line count.

**Three job kinds, one table, `exercise_job`, with the result as JSONB on the row.**

- **build**: staff, one per version. It runs the solution twice on every input (to catch
  non-determinism) and the starter once, then writes the document and the problems. Errors fail
  the build: a crash, a timeout, a failure where no `error` was declared, a solution that does not
  compile (the diagnostics are the detail), or one that does not end with the declared error (so
  `error: ValueError` in a C file fails naming the signal it dies of). Warnings do not: a starter
  that already passes or does not compile, a non-deterministic solution, every test hidden.
- **check**: a recorded attempt. Every test runs on the one prepared program, concurrently, and the
  result holds a verdict per test plus `passed`.
- **run**: the student's own stdin, given up front (Run is not interactive), with nothing
  compared; the page shows the stdin above the transcript.

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
shows a student only its verdict and, on an error, its name if `HINTS` knows it, else `error`
(`_shown_error_name`): the name is the program's to choose, and a fixed list bounds what it can
say. Its output, exit code and running time are channels the program controls, and are withheld.
The page
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
