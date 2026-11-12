---
status: done
---

# Reference solutions

## The problem

A teacher has no way to say "this is what a good answer scores, and this is how fast it runs".
They can submit as themselves, but that run shows up as one more student in the standings, with
their nickname on the leaderboard, and the class can't tell it apart from a classmate.

What we want:

1. From the **challenge editor**, a teacher adds **any number of reference solutions**, each in any
   language from `languages.toml`, each with a label ("Naive O(n²)", "Teacher's C++").
2. They are **graded by the judge like any submission** and their outcomes are recorded.
3. They **show on the leaderboard** (and on the staff standings), **clearly different** from
   students, and they never displace a student.
4. Staff decide per reference what students see: nothing, the metrics, or the metrics and the
   source.

## Decisions (from the interview)

- **A reference is a flagged `submission` row.** `submission.reference_label` (nullable `String`)
  is the flag: non-NULL means reference. `user_id` is the staff member who added it (audit only;
  nothing displays it to students). So the judge, `regrade_challenge`, the submission page, the
  outcome redaction and the deletion cascade all work unchanged, and **every student-facing read of
  `submission` gains `reference_label IS NULL`**. That filter is the whole cost of this choice, and
  step 2 is about paying it everywhere.
- **`submission.reference_visibility`**: a new enum `ReferenceVisibility` with `hidden` (staff only,
  the default for a new reference), `results` (on the leaderboard: label, language, metrics) and
  `source` (as `results`, plus a link to the submission page with the code). One column rather than
  two booleans, because "source but not listed" is meaningless. A check constraint keeps the two
  columns in step: `(reference_label IS NULL) = (reference_visibility IS NULL)`.
- **Input is a file upload**, exactly like a student's: extension → language through
  `EXTENSION_TO_LANGUAGE`, the same 1 MB limit and UTF-8 check. A blank label defaults to the file
  name, so there is no "label required" refusal.
- **The code is immutable.** Label and visibility can be edited; to change the code, add a new
  reference and delete the old one, so the outcomes on screen always belong to the code on screen.
  Each reference has a **Re-run** button.
- **Re-running is manual**, as for students: the challenge-wide **Regrade** requeues references too
  (it already does, being submissions), and nothing requeues on a test-case change. The editor card
  marks a reference **stale** when the set of active test cases differs from the set it has
  outcomes for. That is exact and needs no timestamp (`testcase` has no `updated_at`).
- **Leaderboard: unranked, interleaved.** A reference sits where it would rank, with no rank number.
  It doesn't consume a rank or one of the top-10 slots, and a reference that ranks below the 10th
  student still appears, after the board. Ties go to the student.
- **Same gate as students.** A reference is on the board only if it passes the challenge's
  `_LEADERBOARD_CRITERIA` qualifier (cleared, plus `elapsed_s IS NOT NULL` on the time board). The
  editor card warns when a graded reference does not clear the threshold, which is the useful signal
  that either the reference or the threshold is wrong.
- **Staff standings show references too**, as the same unranked marker rows, hidden ones included
  and marked *staff only*.
- **`cocco pull` / `push` ignore references** in this plan. A push leaves them alone; the zip format
  doesn't change. Follow-up, noted at the end.

### Decisions taken without asking (say so if any is wrong)

- **The judge never learns about references**, in the spirit of "never learns about archived,
  hidden or blocked": `pull_queued_submission` and `SubmissionRunData` are untouched, and references
  share the challenge queue's priority. A regrade therefore grades references in queue order with
  everything else.
- **Archived challenges still accept references.** Adding one is editing, like an exercise build,
  so `post_reference` doesn't join `post_submission`'s archive check. The "three enforcement
  points" of archiving stay three.
- **A reference's outcomes count as outcomes.** `count_testcase_outcomes` (and so `delete_testcase`
  and a push) refuse to delete a test case a reference has been graded on, exactly as for a
  student. Deleting the reference frees it.
- **Deleting a reference destroys its row and outcomes** with an `hx-confirm` (it's the teacher's
  own artefact, not a student's work), and is refused while the judge is running it, like the
  challenge delete. It goes in `repository/lifecycle.py` with the other hand-written cascades.
- **Ties**: a reference ties after a student with the same metric.
- **What a student opening a `source` reference sees** is the normal submission page, under a
  "Reference solution: *label*" heading instead of a person, with `visible_outcomes` redaction
  applied as for any non-staff viewer (hidden test cases' free text stays blank).

## Steps

### 1. Schema

- `cocco/model.py`: `ReferenceVisibility(str, Enum)` (`hidden`, `results`, `source`), and on
  `Submission`: `reference_label` (`String`, nullable) and `reference_visibility`
  (`SAEnum(ReferenceVisibility)`, nullable), plus the `CheckConstraint` above. A comment says what
  the flag means and points at `domain-rules.md`.
- `just migration "reference solutions"`. Per `migrations.md`, check autogenerate's output by hand:
  it creates the enum type on upgrade but **doesn't drop it on downgrade**, and check constraints
  need to be named. Existing rows migrate to NULL/NULL, i.e. nothing changes.
- `repository/rules.py`: `_is_student_submission()` → `Submission.c.reference_label.is_(None)`,
  the one Core spelling. The `text()` queries spell `submission.reference_label IS NULL` and say so
  in a comment, as they already do for `_clears_threshold`.

### 2. Keep references out of everything that is about students

Every reader of `submission` outside the judge, found by grep at planning time:

| Query | Module | Why it must filter |
|---|---|---|
| `query_user_submissions` | `challenges` | a teacher's own history (and `?as=student`) and the staff per-student page |
| `query_challenge_standings` | `challenges` | `cohort`, `attempts`: otherwise the teacher becomes a student row |
| `query_challenge_leaderboard` | `challenges` | `per_submission`: replaced by step 5 anyway |
| `query_course_people` | `courses` | submission counts on the people page |
| `_course_challenges` | `courses` | the viewer's best score on a challenge card |
| `query_users` | `users` | counts on `/admin` |
| `query_user_profile` | `users` | per-course counts on the profile |
| `_populate_dummy_data` | `web/seed` | check whether it reads or only writes |

Untouched on purpose, and the plan says why in each docstring: `pull_queued_submission`,
`requeue_submission`, `mark_submission_done` (the judge grades references), `regrade_challenge`
and `count_running_submissions` (a regrade covers references and must wait for them),
`lock_*_submissions` and `delete_challenge` (the cascade must take references with it).

Re-grep `submission` in `cocco/repository/` and `cocco/web/` before closing the step, since the
table above is a snapshot.

**Test** (`test_repository.py`): one fixture with a student submission and a reference by a
teacher on the same challenge, then assert each query above sees the student's and not the
reference. One test per query, named after it, so a new reader that forgets the filter has an
obvious template to copy.

### 3. Repository: the reference's own reads and writes

In `repository/challenges.py` (and `lifecycle.py` for the delete):

- `insert_reference(conn, user_id, challenge_id, language, filename, source, label)`: like
  `insert_submission` (same insert, `reference_visibility=hidden`, `notify_queue`), doesn't commit.
  Better: give `insert_submission` an optional `reference_label` and have `insert_reference` be a
  thin wrapper, so there's one insert.
- `query_challenge_references(conn, challenge_id)`: one `json_agg` document for the editor card,
  one row per reference in `id` order: id, label, language, filename, visibility, status, score,
  elapsed_s, n_tests, `cleared` (via `_clears_threshold`), and `stale` (the active test-case ids ≠
  the outcome test-case ids, only meaningful once `done`).
- `query_reference(conn, challenge_id, submission_id)`: the single-row read every mutation route
  starts with. NULL when the id isn't a reference **of this challenge**, so a route can't be pointed
  at a student's submission or at another course's reference.
- `update_reference(conn, submission_id, *, label=None, visibility=None)`: writes only what it's
  given.
- `requeue_reference(conn, submission_id) -> bool`: locks the row `FOR UPDATE`; if `running`,
  returns False; else deletes its outcomes, sets `queued`, NULLs `started_at`/`finished_at`, and
  calls `notify_queue`. Same shape as one row of `regrade_challenge`.
- `lifecycle.delete_reference(conn, submission_id) -> bool`: lock first (submission before
  testoutcome, the judge's order), refuse while `running`, then delete outcomes and the row.

### 4. The editor card

`challenge-edit.jinja` gets a **Reference solutions** card after Test cases, as
`f-challenge-references.jinja` (one swap target, `#references`, `hx-swap="outerHTML"`, answered by
every route on it, alert included, per the htmx rules).

- An upload form: file input + label input + **Add**. It sits *inside* the card since nothing on
  the card is filtered.
- One row per reference: label (inline edit), language badge, filename, a visibility `<select>`
  (*Staff only* / *Leaderboard* / *Leaderboard and source*), status or score badge (the existing
  `_score-badge.jinja`, so cleared/failed colours match), running time, a **stale** badge, a
  **doesn't clear the threshold** warning, a link to the submission page, **Re-run** and
  **Delete** (`hx-confirm`).
- While any reference is `queued`/`running`, the card carries `hx-trigger="every 3s"` and
  `hx-get` on its own fragment, so it updates the way a submission does, and stops polling when
  everything is done. **Polling must not wipe a half-typed label**: the label edit is a form
  outside the polled element, or the poll is suspended while a form in the card is `data-dirty`.
  Choose during implementation; the second is less markup.
- It is **not** a `form[data-card]`: each control saves itself immediately, so the unsaved-changes
  hint has nothing to track.
- ? button: `help("challenges", "references", "Help: Reference solutions")`.

Routes in a new module `cocco/web/references.py` (an `APIRouter`, logger
`cocco.web.references`), all behind `ChallengeEditor`, all answering the card:

- `GET  /f/references/{course_id}/{challenge_id}` (the poll)
- `POST /courses/{course_id}/{challenge_id}/references` (add)
- `POST /courses/{course_id}/{challenge_id}/references/{submission_id}` (label and/or visibility)
- `POST /courses/{course_id}/{challenge_id}/references/{submission_id}/rerun`
- `POST /courses/{course_id}/{challenge_id}/references/{submission_id}/delete`

All are four or more segments, so nothing collides with `/courses/{course_id}/{challenge_id}`;
`test_no_route_is_shadowed_by_an_earlier_one` checks that anyway. A `submission_id` that
`query_reference` doesn't find is a 404.

**Upload parsing is shared**: pull the file name / size / extension / UTF-8 checks out of
`post_submission` into one helper in `challenges.py` returning `(filename, language, source)` or
an error sentence, and have both routes call it, so a reference and a submission can't drift on
what's accepted.

### 5. Leaderboard

`query_challenge_leaderboard` gets `include_hidden_references: bool`. Its SQL:

- `per_submission` filters `reference_label IS NULL` (students only), as now.
- A new `refs` CTE: every **done** reference of the challenge, with the same metrics and `cleared`,
  `WHERE {qualifier}`, and `reference_visibility <> 'hidden'` unless including hidden. **No
  `DISTINCT ON user_id`**: one teacher may own several references.
- The final select is `board` (unchanged, still `LIMIT :limit`) `UNION ALL` the refs with
  `rank = NULL`, `is_reference = true`, `label`, `visibility`, and `nickname = NULL`, aggregated
  `ORDER BY {order}, is_reference, submission_id`. The limit applies before the union, which is
  what keeps references out of the top-10 count and still lists a slow one after the board.

`get_fragment_leaderboard` passes `include_hidden_references=role in EDITOR_ROLES`
(`require_challenge_member` already returns the role).

`f-leaderboard.jinja` branches on `entry.is_reference` **before** computing `is_me` (the author is
the viewing teacher):

- no rank number (an em dash or a small flag icon in the rank column), no avatar (a "code" glyph in
  its place), the label in italics with a **Reference** badge and the language;
- a dashed outline / muted background, defined once in `assets/styles.css` as a component class
  (no runtime-assembled class names);
- a link to `/submissions/{id}` only when `visibility == 'source'` or the viewer is staff;
- staff see a *staff only* badge on a hidden one.

The empty state ("Nobody has reached N points yet — be the first.") must still show when the only
rows are references: it becomes "no student row", not "no row".

### 6. Staff standings

`query_challenge_standings`: `cohort` and `attempts` filter to students (step 2), plus a `refs` CTE
(every reference, hidden included, done or not) appended with `rank = NULL`, `is_reference`,
`label`, `visibility`, sorted into the same `ORDER BY cleared DESC NULLS LAST, score DESC NULLS
LAST, elapsed_s ASC NULLS LAST`, with the label where the nickname breaks ties. A reference that
hasn't been graded sorts with the pending students at the bottom. `challenge-staff.jinja` renders
those rows with the same component class as the leaderboard, with no attempts/pending columns and
the visibility as a badge.

### 7. The submission page

- `authorize_submission`: add one branch. A reference whose visibility is `source` is readable by
  anyone with a role on its course (`effective_challenge_role` is not None) **and** for whom the
  challenge is visible (call the same check `require_challenge_visible` uses, so a hidden
  challenge's reference source can't be read by id). Anything else about a reference is staff only
  (the author branch must **not** grant a non-staff author, and can't arise in practice, but a
  demoted teacher shouldn't keep reading their hidden reference). The function still returns
  `is_staff`, so `visible_outcomes` redacts for students.
- `query_submission` / `query_submission_outcomes` return `reference_label`;
  `submission.jinja` shows "Reference solution: *label*" instead of the author, and staff get a
  link back to the editor card.
- Unit tests in `test_main.py`: a student can read a `source` reference, gets 403 on `results` and
  `hidden`, gets 404/403 when the challenge is hidden, and sees hidden test cases' free text
  blanked.

### 8. Docs, seed, guides

- `cocco/docs/challenges.md`: a `## Reference solutions {#references}` section: what they're for,
  the three visibilities in **bold UI labels**, that they never take a student's place, that
  editing code means re-adding, and what **stale** means. `students.md`: one paragraph on the
  marked rows in the leaderboard.
- `cocco/web/seed.py`: one `results` reference on a seeded challenge, so the styling is visible in a
  fresh dev database.
- Guides: `domain-rules.md` (a "references" bullet: the flag, the filter, the judge doesn't know,
  outcomes block test-case deletion, interleaving), `pages.md` (the editor card and the
  submission page branch), `authorization.md` (the new `authorize_submission` branch),
  `data-model.md` (the two columns and the constraint), and CLAUDE.md's *Data model* paragraph
  (one clause: "a submission with `reference_label` is a teacher's reference solution").
- `CHANGELOG` entry.

### Tests (beyond those named above)

- `test_repository.py`: leaderboard interleaving on both rankings: a reference between two
  students has no rank and the students are ranked 1, 2; a reference slower than the 10th student
  is still returned; a hidden one only with `include_hidden_references`; an uncleared one never.
  A teacher with two references gets two rows.
- `test_repository.py`: `requeue_reference` and `delete_reference` refuse while running;
  `query_reference` is NULL for a student's submission and for another challenge's reference;
  `stale` flips when a test case is added or retired.
- `test_main.py`: each references route, refused for a student (403) and for staff of another
  course; the card's alert on a bad extension; the add route enqueues and notifies.
- `test_judge.py` (integration): one end-to-end reference graded through `run_from_queue`, to
  prove the judge really doesn't care.
- `test_docs.py` picks up the new `help()` call by itself.

## Out of scope / follow-ups

- **Transfer**: a `references/` directory and a `[[references]]` manifest table (label, file,
  visibility), with a push matching references by file name and refusing to silently destroy graded
  runs, in the spirit of `plan_testcases`.
- Comparing a student's submission with a reference per test case (a "vs reference" column on the
  submission page).
- Automatically requeuing references on test-case changes, if manual turns out to be a chore.

## Implementation summary (2026-11-12)

Done, steps 1–8, each by an implementer subagent and reviewed before the next. Full suite: 1418
passed (integration included). Migration `bcb96738a3db`.

Where the code differs from the plan, or was corrected in review:

- **Poll suspension** (step 4): htmx 4 beta2 doesn't parse a filter on `every 3s [expr]` (it
  silently drops it), so the card polls with a plain `every 3s` and a `htmx:before:request`
  listener in `challenge-edit.jinja` cancels the poll while a `.ref-text` box is edited or a
  `.ref-file` has a file. Not yet checked in a browser.
- **Blank label on rename** is refused with an alert (on add it still defaults to the file name).
- **Route tests** live in `cocco/test_reference_routes.py` (the `make_client` pattern), not
  `test_main.py`, which has no database fixtures.
- **`query_submission`'s attempt number** also counted references; it now counts students only,
  and a reference has no attempt number (found in step 2, fixed in step 7).
- Review fixes: `query_challenge_references` aggregated every `testoutcome` in the database (now
  restricted to the challenge's references); leaderboard reference rows lacked the language.
- Seed: a `results` reference, "Teacher's Dijkstra", by the dummy admin.

Noticed, not touched: the unsaved-changes script in `challenge-edit.jinja` listens for
`htmx:afterRequest`, an htmx 2 event name; under htmx 4 the statement card's dirty flag may never
clear after a save.
