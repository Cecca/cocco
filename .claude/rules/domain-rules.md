---
paths:
  - "cocco/repository/**"
  - "cocco/web/challenges.py"
  - "cocco/web/challenge_editor.py"
  - "cocco/web/courses.py"
  - "cocco/web/testcases.py"
  - "cocco/model.py"
  - "cocco/judge.py"
  - "cocco/transfer.py"
  - "cocco/templates/**"
---

# Domain rules

Moved from `CLAUDE.md`'s *Conventions*: ordering, test cases, regrading, deletion, archiving, visibility, blocks, scores.

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
- **A block stops a person, it doesn't retract their work**: queued submissions are graded, and standings and leaderboards keep listing them. Nothing blocked is seeded (a blocked seeded admin would lock out a fresh checkout). **Removing an enrollment draws the same line**: `delete_enrollment` takes one row. Submissions and outcomes stay, because `query_challenge_standings`'s cohort already unions in everyone who submitted.
- **Scores are per test case, in `[0, 1]`**; the headline is their mean ×100. That assumes higher-is-better, and nothing enforces the range.
- **The leaderboard criterion is per challenge**: `challenge.ranking` drives `query_challenge_leaderboard`, selecting one of `_LEADERBOARD_CRITERIA`'s literal SQL variants by enum, never request text. `running_time` admits only submissions passing *every* test, ranked by summed time. `total_score` / `average_score` rank by score and admit partial credit. Each student appears once (best submission), and every row carries all three metrics plus `n_tests`.
- **A hidden test case gives away nothing but its verdict.** `visible_outcomes` in `cocco/web/challenges.py` blanks `message`, `stdout`, `stderr` unless the case is a sample or the viewer is staff: stdout/stderr can echo the hidden input, and `message` comes from an evaluator that read the hidden input and expected output (`diff.py` prints the expected output). Verdict, score and resource usage are always shown, and compiler output is exempt. Redact in that function, not templates, so it stays unit-testable.
