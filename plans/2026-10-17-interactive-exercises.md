---
status: pending
---

# Interactive exercises, paced by the teacher

## Goal

Bring the `esercizi-python` prototype (`~/Work/esercizi-python`) into cocco, wooclap-style:

1. **Short Python exercises run by the judge**, not in the browser. Hidden tests stay on the server,
   and "solved" is a verdict the server reached rather than something a student's localStorage claims.
2. **The teacher sets the pace.** In a live session students can work only up to the exercise the
   teacher has unlocked, and the teacher moves the class on.
3. **The teacher sees the classroom.** For every exercise: how many students solved it, how many
   tried and are not there yet, and how many have not tried. Per student, with names, on the
   teacher's screen; as anonymous counts on the projector.
4. **One set, many sessions.** The same exercise set can be run with lab group A on Monday and
   group B on Wednesday, and each run has its own pacing and its own progress.

Purely formative: nothing is graded, nothing is exported, no leaderboard. Python only for now.

## Where things stand

- **The prototype** is a static site. `formato.js` parses a Markdown format (front matter, one
  `# Title` per exercise, fenced blocks `iniziale` / `soluzione` / `input [nascosto]` / `output` /
  `errore` / `suggerimento`, `confronto:` and `tempo:` lines). `autore.js` runs the solution in
  Pyodide to compute expected outputs. `comune.js` holds the output comparison (`esatto`, `spazi`,
  `maiuscole`, `numeri <tol>`, `ordine`), the Italian error hints, and the input() handling:
  prompts are dropped when checking and echoed when running. Its README says what it cannot do:
  hidden tests can be decoded, and "solved" can be forged.
- **cocco's judge cannot serve a classroom as it is.** `run_loop` (`cocco/judge.py:1115`) takes
  **one submission at a time**. `run_submission` runs its tests **one after another**, and an idle
  loop **sleeps 5 s** before looking again. If 100 students press Check within one minute, with 5
  tests each and a full `isolate --init` / run / `--cleanup` per test, the last student waits for
  minutes.
- `pull_queued_submission` (`cocco/repository.py:95`) claims a row with `UPDATE … WHERE id =
  (SELECT … LIMIT 1)`. That is enough for one judge doing one thing at a time. Under concurrent
  claimers two of them can claim the same row: the subquery is not re-evaluated when the
  second UPDATE waits on the first.
- The existing layers fit well: `Language`, `isolate_run`, `_IDS_POOL` and the prewarmed runtimes
  are reused as they are. Challenges, submissions, test cases, evaluators and leaderboards are not.

## Design decisions

### Exercises are their own tables, not a kind of challenge

A challenge is built for big inputs on disk, evaluator programs, resource limits and leaderboards.
An exercise is small: a few short inputs, starter code, hints and a declarative comparison rule.
Forcing the two into one table would put a "but not for exercises" branch into every standings,
leaderboard, transfer and regrade path. They share the **execution layer** and nothing above it.

### The `.md` file is the source of truth; everything else is compiled from it, once

A set is authored as one Markdown file, and the server stores **that text verbatim**, solution
included. Only staff ever see it. There are **no `exercise` / `exercise_test` tables**. Keeping
the parsed fields in rows next to the source would be two copies of the same facts, and the first
route that wrote one and not the other would make them disagree in silence.

Instead, every import creates an **`exercise_set_version`**: the source text plus the **compiled
document** derived from it. The compiled document is the parse, plus the expected outputs the
judge computes (see "The build"), stored as one JSONB value. It plays the role of the prototype's
`.json`, solutions included because the server needs them. **A version is immutable.** Its source
is written when it is created. Its compiled document and status are written exactly once, by the
build that finishes it (`building` → `ready` or `failed`), and never touched again. Two values in
one row that are both written once and never updated cannot drift apart. That replaces a rule
anybody has to remember.

- **The set** holds what is not content: the course, `archived_at`, and a `current_version_id`
  pointing at the latest **ready** version. A failed re-import therefore breaks nothing: the set
  page shows the failed version's problems, and everything keeps using the current one.
- **A session pins a version** when it is opened (`exercise_session.version_id`). This is the
  "move the parsed information into the session" idea, without copying it: sessions of the same
  version share one row, and a session's content is fixed unless the teacher changes it.
- **A job records the version it ran against.** A check's result is always read against the tests
  it was actually graded on, including on the dashboard's drill-in after the session has moved on.
- **An exercise is identified by its position in the version** (`exercise_index`, 1-based, the
  `{n}` in the URL). There is no exercise id because there is no exercise row.
- The source's other uses come for free. Re-export is lossless, `cocco pull`/`push` of a set is
  one file, and the solution exists on the server and can be **revealed** to the class after an
  exercise.
- Old versions are kept: a few KB each. Pruning the ones no session or job references is a
  follow-up, if it ever matters.

This is also where "test data lives in the database" comes from, deliberately. The rule that
"large inputs live on disk" exists for million-line inputs. Exercise inputs are a few bytes typed
into a Markdown file, and keeping them in the compiled document lets the judge read an exercise's
tests in one query and keeps `Storage` out of this feature entirely. CLAUDE.md should say so in as
many words, so that nobody "fixes" it.

### The format, with English keywords

The prototype's format is kept in structure. Its keywords are **English**, with one spelling and
no Italian aliases. The existing `sorgenti/*.md` files are converted once, outside this repo.

| Prototype | cocco |
|---|---|
| front matter `titolo:` / `descrizione:` | `title:` / `description:` |
| ` ```iniziale ` | ` ```starter ` |
| ` ```soluzione ` | ` ```solution ` |
| ` ```input ` / ` ```input nascosto ` | ` ```input ` / ` ```input hidden ` |
| ` ```output ` | ` ```output ` |
| ` ```errore ` | ` ```error ` |
| ` ```suggerimento ` | ` ```hint ` |
| `confronto:` with `esatto`, `spazi`, `maiuscole`, `numeri 0.01`, `ordine` | `compare:` with `exact`, `spaces`, `case`, `numbers 0.01`, `order` |
| `tempo: 5` | `time: 5` |

As before, `python starter` / `python solution` also work, any other code block stays part of the
statement, and an exercise with no `input` gets one test with empty input.

### Expected outputs are computed by the judge: the build

Importing a file parses it synchronously: a syntax problem is a 200 with an alert, which is
htmx's rule, and no version is created. A file that parses becomes a `building` version, and a
**build** job is queued. The judge runs the solution on every input, twice so that random output
is caught, and the starter on every input. It writes the compiled document, the problems
`autore.js` reports today, and the final status:

- errors: the solution crashes, times out, or raises on a test without `error`;
- warnings: the starter already passes, the solution is non-deterministic, every test is hidden.

A ready version becomes the set's current one. A session can only be opened on, or moved to, a
ready version.

### A Python harness runs inside the box

Two of the prototype's semantics are about Python itself:

- prompts written by `input("…")` must not be compared;
- `error: ValueError` passes only when the program ends with that exception or a subclass.

So nothing runs the student's file directly. A bundled `cocco/exercises/harness.py`, shipped in
the package like the evaluators, is copied into the box and run as `python harness.py <mode>
main.py`. It:

- replaces `input()`. In `check` mode it drops the prompt. In `run` mode it echoes the prompt and
  the line read, so the transcript reads like a terminal;
- captures stdout and stderr in buffers capped at 256 KB, as `PG.CHECK_LIMIT` does. An endless
  print stops at the cap instead of filling memory;
- compiles the student's code under the name `main.py` and runs it, so tracebacks and line
  numbers refer to the student's own file;
- catches everything and always exits 0, writing **one JSON object** on the real stdout:
  `stdout`, `stderr`, `truncated`, and `exception: {type, mro, line, traceback}` or null.

**The isolate status says how the run ended; the JSON says what the program did.** A timeout or
an OOM is an isolate status and produces no JSON. A missing or unparseable JSON with status `OK`
is a harness failure, which is a bug.

A student can forge that JSON by writing to fd 1 directly. That is accepted: the comparison
against the expected output still happens in the judge, so the only thing a forgery can fake is
the exception check, and this feature grades nothing.

### Comparison happens in the judge process, not in a box

The comparison rules are declarative and the code that applies them is ours. Running an evaluator
program in isolate for every test, as challenges do, would double the per-test cost to get nothing.
`cocco/exercises/compare.py` is a port of `parseRules`/`compare` from `comune.js`, under the
English rule names, and is tested against cases lifted from the prototype's behaviour.

### Run and Check both go through the judge, as jobs with one table

There are three kinds of work:

- **build**: staff, per version;
- **check**: a recorded attempt;
- **run**: the student's own stdin, nothing compared, nothing recorded.

They are one table, `exercise_job`, with a `kind`, the `version_id` it runs against, the
`exercise_index`, and the result as **JSONB on the row**:

- **No child rows** means the judge writes its result with an `UPDATE` of a row that may have been
  deleted meanwhile. Zero rows updated is harmless. Contrast `insert_test_outcomes`, whose foreign
  key violation takes the judge process down. That is the race the challenge delete has to lock
  against; this design does not have it.
- **Run jobs are kept for six months**, the same retention the logs have, and the judge prunes
  older ones on its idle ticks. They count towards nothing: progress queries read
  `kind = 'check'` only, behind a partial index. Keeping them for a term means a teacher can see
  what a student tried before they dared press Check.
- **Preview jobs are jobs without a session.** Staff try a version as a student would, before
  opening a session on it. `session_id` is NULL, so no progress query ever sees them.
- **A student has at most one job queued.** A new Run or Check replaces their queued one. It does
  not touch one that is already running. Mashing the button costs the judge nothing, and "queued"
  is never a backlog of stale requests.
- **Run is not interactive.** Stdin is the text box's contents, given up front, and the harness
  echoes what was read so that the transcript still reads like a terminal. The prototype's
  "type as you go" is the one thing this choice gives up.

### The judge learns concurrency, a wake-up and a priority: phase 1, which helps challenges too

1. **Concurrency.** `[judge] concurrency` jobs in flight (default 8), each a task claiming and
   running a job. The tests of one exercise job run concurrently. The box pool stays the real cap.
2. **One box per job, not per test**, for exercise jobs. `isolate --init` once, then one `--run`
   per test in the same box, then `--cleanup`. Init and cleanup are a large share of the cost of a
   20 ms Python program. Step 0 measures whether this is worth its complexity before it is built.
3. **`FOR UPDATE SKIP LOCKED`** in every claim, `pull_queued_submission` included. This is
   required as soon as there is more than one claimer.
4. **`LISTEN cocco_queue`.** The web app runs `pg_notify` in the transaction that inserts a job
   or a submission. PostgreSQL delivers it at commit, so a notification never arrives before its
   row is visible. The judge wakes on it immediately, and the 5 s poll stays as the fallback.
5. **Priority: run > check > build > challenge submission.** A regrade of a challenge with 300
   submissions must not freeze a live lesson, and a student staring at Run is the most
   latency-sensitive person in the building.

The judge's engine stays synchronous. A claim is a millisecond of blocking per job. If the
measurements say otherwise, the claim moves to `asyncio.to_thread`.

**Target:** 100 students pressing Check within 10 s on a 5-test exercise, p95 under 3 s from
click to verdict, on the lab's judge host. Step 11 checks it.

### Sessions carry pacing and progress; a set is only content

- **`exercise_session`**: belongs to a set and pins a `version_id`. It has a label ("Lab A — 20
  Oct"), a **mode** (`live` or `open`), `unlocked_through` (how many exercises are unlocked, only
  meaningful when live), `opened_at` and `closed_at`.
- **Live mode:** students see and attempt exercises `1..unlocked_through` and may go back to
  earlier ones. A student who finishes early sees "wait for the teacher", not the next statement.
  The teacher has *next* / *previous* / *unlock all* buttons.
- **Open mode:** everything is unlocked and there is no pacing. This is the homework mode, and a
  live session can be switched to it after class.
- **Closed:** read-only. Students can still review their code and results; nothing new is accepted.
- **Several open sessions of one course are allowed.** The course page lists the open ones, and a
  student picks one. cocco has no groups, and inventing them for this would be scope creep.
- **Pacing is enforced on the server**, in the dependency every exercise route goes through,
  the way `require_challenge_visible` is. A job for a locked exercise or a closed session is
  refused. Hiding a button is presentation.
- **Participants.** `exercise_session_participant(session_id, user_id, joined_at)` is written on a
  student's first visit (`ON CONFLICT DO NOTHING`). It is the dashboard's denominator: the
  students who came, not the students enrolled on the course.
- **Code does not carry over between sessions.** Every session starts from the starter code. A
  student's last checked code is per session and per exercise.
- **Revealing a solution** is per session and per exercise
  (`exercise_session_reveal(session_id, exercise_index, revealed_at)`). Once revealed, students
  see the solution under the statement, and the projector can show it.

### Fixing an exercise during a session: move the session to a new version

This is a real case: the prototype's README tells the teacher to fix an exercise during class and
re-upload it. Re-importing the file creates a new version and, once it is ready, makes it the
set's current one. **Open sessions do not change by themselves.** The session's staff page offers
"switch to the latest version", and that is the only write that ever changes
`exercise_session.version_id`.

- **The switch refuses a version with fewer exercises than this session has checks for**, naming
  the exercise. Exercises are matched by position, and a check on exercise 5 needs an exercise 5
  to mean anything.
- **Everything else is allowed:** a changed statement, a fixed test, a new solution, exercises
  appended at the end. The confirmation lists the exercises whose title changed, because a
  reordered file is the one mistake position-matching cannot catch.
- **Past checks keep their results** and the version they were graded on. "Solved" stays solved.
  Nothing is regraded, because nothing is graded.
- **`unlocked_through` is clamped** to the new exercise count, which can only stay or grow.

### Progress is derived, never stored

A student's status on an exercise in a session is computed from their check jobs in that session:

- **solved**: at least one check passed every test. *Solved is sticky*: a later wrong attempt does
  not undo it, and neither does a version switch;
- **attempted**: at least one check, none passed;
- **not attempted**: no check yet;
- **checking**: a check is queued or running. Shown beside the status, not instead of it.

One raw SQL query, in this repo's `json_agg` style, returns the whole dashboard document: per
exercise, the counts in each status; per participant, the status of each exercise. The exercises'
titles come from the pinned version's document (`jsonb_array_elements … WITH ORDINALITY`). The
cohort is the participants unioned with anyone who submitted a check, like
`query_challenge_standings`.

### Two staff views of a session

- **Dashboard** (`…/dashboard`): names, a grid of participants by exercises, polled every 3 s.
  Clicking a cell opens that student's latest code and results.
- **Projector** (`…/projector`): no names. One bar per exercise (solved / attempted / not
  attempted, out of the participants), the pacing controls, and the reveal button. It is a
  separate page because a projected screen must never show who is stuck.

Showing one anonymous wrong attempt on the projector to discuss it is a natural follow-up, since
attempts keep their source. It is **not** in this plan.

### The student page

- **An exercise strip** coloured by status, like the prototype's, plus statement, editor, hints
  (revealed one at a time, client-side; hints are not secret), and the **Run** (stdin) and
  **Check** tabs.
- **One page per exercise:** `…/sessions/{session_id}/{n}`. The poll **never swaps the editor**.
  It swaps only the strip, which is a tiny fragment, every 3 s. On a "wait for the teacher" page,
  which has no editor to lose, the poll answers with an `HX-Redirect` to the exercise once it
  unlocks.
- **The student never receives the compiled document.** One function builds the student's view of
  an exercise from it: statement, starter, hints, the visible tests with their expected outputs,
  and the solution only once it is revealed. Hidden tests and unrevealed solutions never leave the
  server. That is `visible_outcomes`'s rule, kept in a function rather than in the template.
- **The draft is kept in localStorage**, keyed by session and exercise, so moving between
  exercises or reloading loses nothing. A copy goes to the server with every Run or Check, and the
  page uses it when localStorage is empty, for example after a change of computer.
- **Results:** Check shows, for each visible test, the input, the expected output and the
  student's output, with differing lines marked. For a hidden test it shows only the verdict and,
  on an error, the exception type and line. An error comes with a short explanation:
  the prototype's `errorHint`, ported to `cocco/exercises/hints.py` and **translated to English**,
  like the rest of the interface.
- **Editor:** CodeMirror 5, vendored under `cocco/static/` as the prototype does it. Version 6
  needs a bundler, and this repo has none.
- **Polling for a job's result:** the fragment polls every 500 ms while the job is pending and
  stops with htmx's 286 once it is done.

### Authorization and URLs are course-keyed

Every route lives under `/courses/{course_id}/…`:

- sets: `/courses/{course_id}/exercises/{set_id}/…`;
- sessions: `/courses/{course_id}/sessions/{session_id}/…`.

The role is resolved from `course_id`, as everywhere else, with an `*_in_course` cross-check like
`challenge_in_course`:

- **staff** (`CourseEditor`) for importing, previewing, opening, pacing, switching versions,
  revealing, the dashboard and the projector;
- **members** for the student page and for creating run and check jobs.

The two-segment routes collide with `/courses/{course_id}/{challenge_id}` and are **declared above
`get_challenge`**, like the editor and the people page.

**Archived courses:** a job on an archived course is refused. That makes the job route a **third
enforcement point** for archiving, next to `post_submission` and `post_course_enroll`, and the
CLAUDE.md sentence that says "two" changes with it. **A blocked student** is refused by
`get_user`, like everywhere else. Jobs they queued before the block are still run, for
`pull_queued_submission`'s reason.

### Deletion

`delete_course` gains the exercise tables in its hand-written cascade, in this order: jobs,
reveals, participants, sessions, then the set's `current_version_id` set to NULL, versions, sets.
There is no `ON DELETE`, consistent with the rest of `model.py`. Because results are JSONB on the
job row, a judge finishing a job whose course has just been deleted updates zero rows instead of
crashing. So, unlike submissions, no lock is needed.

A set can be deleted only when it has no sessions. Deleting a session deletes its jobs,
participants and reveals: this is formative work with nothing to keep a record of.

## Steps

### [ ] 0. Measure before building

Time a trivial Python program through `isolate_run`, cold and warm: init + run + cleanup versus a
second `--run` in an already initialised box. Time 100 checks of 5 tests with the current
`run_loop`. These numbers decide whether "one box per job" (judge design, item 2) is built, and
they are the baseline for step 11. Record them in this plan.

### [ ] 1. Judge throughput

`[judge] concurrency`, with `config.py` validation and a `cocco.example.toml` entry. Worker tasks.
`SKIP LOCKED` in `pull_queued_submission`. `LISTEN`/`NOTIFY`, with the `pg_notify` in
`post_submission` and `regrade_challenge`. The priority hook that exercise jobs plug into in
step 4. This ships on its own, and challenges get faster from it.

Tests: two concurrent claimers never get the same row; a notification wakes an idle judge well
under the poll interval; concurrency never exceeds the box pool.

### [ ] 2. The pure modules, `cocco/exercises/`

- `format.py`: a port of `formato.js` with the English keywords. It parses a file into the
  compiled document's shape, minus the expected outputs, or returns a list of problems.
- `compare.py`, `hints.py` and `harness.py`.

They touch no database, FastAPI or judge, which is the seam `transfer.py` already uses. Unit tests
by import, and the harness is also run directly with `python`, outside isolate. The fixtures are
written for the tests; the files in `esercizi-python/sorgenti` contain real solutions and are not
copied in.

The **compiled document's shape** is pinned here, as a Pydantic model in `cocco/schema.py`, so
that the build that writes it and every reader of it agree on one definition. It carries a
`format` version number, as the prototype's `.json` does.

### [ ] 3. Schema and migration

New tables:

- `exercise_set`: course, `current_version_id` (nullable), `archived_at`;
- `exercise_set_version`: set, source text, title (from the front matter, for listings), status,
  problems JSONB, compiled document JSONB;
- `exercise_session`: set, version, label, mode, `unlocked_through`, `opened_at`, `closed_at`;
- `exercise_session_participant` and `exercise_session_reveal`;
- `exercise_job`: kind, status, user, version, session (nullable), `exercise_index` (nullable for
  a build), source, stdin, result JSONB, `enqueued` / `started_at` / `finished_at`.

`exercise_set.current_version_id` and `exercise_set_version.set_id` point at each other. The
foreign key on the set is therefore added after both tables exist (`use_alter`), and the cascade
clears it first.

New enums: `exercisejobkind`, `exercisejobstatus`, `exercisesessionmode` and
`exerciseversionstatus`. Timestamps are `timestamptz` with `server_default=func.now()`. Indexes:
the partial index on check jobs by `(session_id, exercise_index, user_id)`, and the queue index on
`(status, kind, enqueued)`.

The revision is autogenerated, **then read**: the server defaults are invisible to autogenerate.
Its `downgrade` drops the four enum types explicitly. `downgrade base` then `upgrade head`, and
the drift test passes.

### [ ] 4. The judge runs exercise jobs

Claim with priority across `exercise_job` and `submission`, using `SKIP LOCKED`. The tests come
from the job's version, out of the compiled document, in one query.

- **build:** solution twice and starter once on every test; write the compiled document, the
  problems and the status in one `UPDATE` (only while the version is still `building`); on
  success, move the set's `current_version_id` in the same transaction.
- **check:** every test through the harness, then compared in-process; the result JSONB holds a
  verdict per test plus an overall `passed`.
- **run:** once, transcript back.

Prune run jobs older than six months when idle. Integration tests under
`@pytest.mark.integration`: the exception MRO, prompts dropped versus echoed, a timeout, an endless
print, a missing JSON, and a build that fails leaving the current version where it was.

### [ ] 5. Authoring

The course page gets an **Exercises** section for staff. **Import a `.md` file** (an upload, as a
test case is): it parses the file, creates a version and queues its build. The set page shows:

- the latest version's status (a polled fragment) and its problems;
- which version is current;
- a **student preview** of the current version, running on the judge without recording anything;
- a download of the source.

Delete when there are no sessions.

### [ ] 6. Sessions and pacing

Open a session on the current version: label, mode. Then next / previous / unlock all, switch to
open, close, reveal a solution, and switch to the latest version with its refusal and its
title-change confirmation. Students see the open sessions on the course page. The dependency that
enforces membership, `session_in_course`, the unlocked range and closed-ness.

### [ ] 7. The student page

Vendor CodeMirror under `cocco/static/` (licence noted). The strip fragment, the student-view
function, the statement, hints, the Run and Check tabs, the job fragment, drafts in localStorage
with the server copy as fallback, the wait page, and participant registration.

### [ ] 8. Dashboard and projector

The progress query, the named grid with drill-in (read against the version each check ran on),
and the anonymous projector with the pacing and reveal controls.

### [ ] 9. Deletion and archiving

`delete_course`'s cascade, session delete, the archived-course refusal on jobs, and a test that a
judge finishing a job of a deleted course does not crash.

### [ ] 10. Transfer (may slip to a follow-up)

`cocco pull`/`push` of a set as its `.md`, with the existing `.cocco/state.toml` / `ETag` /
`If-Match` discipline. The `ETag` is the current version's id, because a version is immutable;
a push is an import.

### [ ] 11. Verify end to end

`just dev`: import one of the prototype's sets, converted to English keywords, and open a live
session. Drive three seeded students through it while watching the dashboard and the projector,
fix an exercise mid-session and switch the session's version. Then a load script with 100
simulated students pressing Check within 10 s against the real judge, measured against the p95
target. Record the numbers here.

### [ ] 12. Documentation

- CLAUDE.md: a new "Exercises" section covering:
  - the source of truth and immutable versions;
  - the format;
  - the harness contract;
  - the job kinds, priority and retention;
  - sessions and version switching;
  - the DB-not-disk exception to "large inputs live on disk";
  - the third archiving enforcement point;
  - the cascade.
- DESIGN.md: why the judge rather than Pyodide, why exercises are not challenges, and why there
  are compiled versions instead of exercise rows.
- `cocco.example.toml`: `[judge] concurrency`.

## Decided

- Run and Check both execute on the judge (option b). Run is not interactive: stdin is given up
  front.
- Repeated sessions: pacing and progress belong to a session, and a set is only content.
- Purely formative: no grades, no export, no leaderboard, and solved is sticky.
- Python only, through a bundled harness. The `language` field exists in the compiled document
  for later.
- The `.md` source is the single source of truth. Each import is an immutable compiled version, a
  session pins one, and the teacher moves a session to a newer version explicitly.
- English keywords in the format, one spelling, no Italian aliases.
- Run jobs are kept for six months.
- The interface is in English, error explanations included.

## Open questions

- **"One box per job"**: decided by step 0's numbers.

## Future work

- **Internationalization.** The student page's labels and the error explanations in `hints.py`
  become translatable when cocco as a whole gains i18n, not as part of this feature.
- Pruning versions that no session or job references.
- Showing one anonymous wrong attempt on the projector for discussion.
