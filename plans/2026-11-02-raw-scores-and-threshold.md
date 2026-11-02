---
status: done
---

# Raw scores and a pass threshold

## The problem

A submission's headline score is the **mean** of its per-test scores, shown ×100 as "`n` / 100"
(`render_score`, `_score-badge.jinja`). That bakes in two assumptions an evaluator never agreed to:
that every test scores in `[0, 1]`, and that every test weighs the same. `EvaluatorResult` already
says nothing enforces the range; the mean is where it quietly goes wrong.

What we want instead:

1. **The evaluator's number is a raw score**, and a submission's score is the **sum** of them. No
   rescaling, no "/ 100". A test worth 10 points says `{"score": 10}`.
2. **Each challenge has a configurable score threshold.** A submission clears it or it doesn't.
3. **The leaderboard counts only submissions that clear the threshold.** The rest are **failed**,
   and are shown as such.

Nothing stored changes meaning: `testoutcome.score` already holds exactly what the evaluator
printed (`judge.py` writes `result.score` untouched). So this is a change to *aggregation and
presentation*, plus one column. **No regrade is needed**, and because the threshold is applied when
reading, editing it takes effect on the next poll.

## Decisions

- **The submission score is `sum(coalesce(score, 0))`** over its outcomes, everywhere a headline
  score is computed. A missing score still counts as zero, as now.
- **`challenge.score_threshold`: `Float`, nullable.** `NULL` means *no threshold*: every graded
  submission clears it, which is exactly today's behaviour of the score boards, so existing
  challenges migrate to `NULL` and nothing moves. Clearing is **`score >= score_threshold`**
  ("reach 10 points"). Any finite number is allowed, negative included (an evaluator scoring a
  negated error can use one).
- **"Cleared" is spelled once**, as a Core fragment in `repository/rules.py`
  (`_clears_threshold(score, threshold)` → `threshold IS NULL OR score >= threshold`), like
  `_challenge_visible`. The `text()` queries that can't call it spell the same expression and say
  so, the way `_score_columns`' docstring already does for its four aggregates. Every page query
  exposes it as a boolean **`cleared`** column, so templates never compare numbers themselves.
- **One score ranking, not two.** `total_score` and `average_score` were "sum" and "mean ×100" of
  the same thing; with raw scores the mean is just the sum divided by a test count the student
  doesn't control. `LeaderboardRanking` becomes `running_time` | `total_score`; existing
  `average_score` challenges become `total_score`. (Renaming `total_score` to `score` would be
  tidier but buys nothing and breaks every pushed `challenge.toml`; not worth it.)
- **The threshold is the only gate, on both boards.** A failed submission never appears on the
  leaderboard, whatever the ranking.
  - `total_score`: qualifier `cleared`, ordered `total_score DESC, elapsed_s ASC NULLS LAST`.
  - `running_time`: qualifier `cleared AND elapsed_s IS NOT NULL`, ordered by time. **`all_passed`
    is dropped** from the qualifier and from `per_submission`. It only ever meant *every test ran*
    (`OutcomeEnum.success` is written for any evaluator result, score 0 included), so it never
    stopped a fast wrong answer anyway. A submission that timed out on a test but still clears the
    threshold qualifies, and the killed run's time counts towards its total. That's intended: the
    threshold decides who qualifies.
- **New challenges have no threshold, and the teacher is warned about it.** With no threshold every
  graded submission counts, wrong answers included, and on the time ranking that means the fastest
  program wins whatever it printed. So wherever a teacher sees the challenge's settings, a
  missing threshold shows as a `alert-warning` (step 4/5): in the editor form, and on the staff view
  of the challenge.
- **"Failed" is a presentation of `NOT cleared` on a graded submission.** It is not a new
  `SubmissionStatus` or `OutcomeEnum` value and nothing is written: the judge never learns about
  thresholds, just as it never learns about archived or hidden. A failed submission is:
  - **left off the leaderboard** (it doesn't qualify);
  - **badged "failed"** — red, with its raw score beside it — in the student's submission history
    (`f-submissions.jinja`), on the submission page (`submission.jinja`) and in the staff standings
    (`f-standings.jinja`). A cleared one is badged green with its score. With no threshold every
    graded submission is cleared, so the badge is green or neutral, never red.
- **The badge colour is no longer by fraction.** `score >= 0.9` / `>= 0.5` presumes a `[0, 1]`
  mean. With raw sums there is no maximum to be a fraction of, so the colour comes from `cleared`
  alone: green if cleared against a real threshold, red if failed, neutral (`badge-ghost`-ish, class
  names spelled out in the template) when the challenge has no threshold.
- **`render_score` prints the raw number**: no ×100, no rounding to an integer. Integers print as
  integers, otherwise up to two decimals with trailing zeros stripped (`7`, `7.5`, `0.33`). The same
  filter serves the per-test column in `f-submission-results.jinja`, which then shows `1` instead of
  `100`.
- **Best attempt** (`is_best` in `query_user_submissions`, `best` in the standings) prefers a
  cleared submission over a failed one, then higher score, then (standings) shorter time. Standings
  rank the class the same way, so a failed best sorts below every cleared one.

## Steps

### 1. Schema

- `model.py`: `Column("score_threshold", Float, nullable=True)` on `Challenge`, with a comment on
  what `NULL` means. Drop `average_score` from `LeaderboardRanking` and rewrite its docstring
  (raw scores, threshold gates both boards).
- `schema.py`: add `score_threshold: float | None` to the challenge mirror(s) (see
  `data-model.md`); reword `EvaluatorResult`'s docstring: the score is raw, summed, no range.
- Migration (`just migration "raw scores and score threshold"`, then hand-edit per
  `migrations.md` — autogenerate sees the column but **not** the enum change):
  - `add_column challenge.score_threshold`.
  - `UPDATE challenge SET ranking = 'total_score' WHERE ranking = 'average_score'`.
  - Recreate `leaderboardranking` without `average_score` (rename old type, create new, `ALTER
    COLUMN ... TYPE ... USING ranking::text::leaderboardranking`, re-set the server default, drop
    the old type). Postgres can't drop an enum value in place.
  - `downgrade`: the reverse; `average_score` rows are not recoverable, say so in a comment.

### 2. Queries (`cocco/repository/`)

- `rules.py`: `_clears_threshold`.
- `challenges.py`:
  - `_score_columns`: `sum` instead of `avg`; docstring.
  - `_submission_outcomes` / `query_submission` / `query_submission_outcomes`: expose `cleared`
    (needs the challenge's threshold; join `challenge` on the submission).
  - `query_user_submissions`: `sum`, `cleared` per attempt, `is_best` ordered `cleared DESC,
    score DESC`.
  - `_LEADERBOARD_CRITERIA` / `query_challenge_leaderboard`: drop `average_score` from the CTEs and
    rows; join the challenge's threshold into `per_submission` and compute `cleared`; qualifiers as
    in *Decisions*. Comment above `_LEADERBOARD_CRITERIA` rewritten.
  - `query_challenge_standings`: `sum`, `cleared`, ordering as in *Decisions*; docstring loses the
    "mean only means anything for higher-is-better" caveat in favour of the threshold.
  - `query_challenge` (and whatever feeds the editor form / statement) returns `score_threshold`.
- `insert_challenge` / `update_challenge` accept `score_threshold`.

### 3. Judge

- `judge.py` around line 1325: the logged headline score becomes the sum. Nothing else; the judge
  stays ignorant of the threshold.
- `evaluators/__init__.py`, the bundled evaluators' docstrings: the score is raw and summed; the
  bundled ones happen to score each test in `[0, 1]`, so a challenge using them has a maximum of
  `n_tests` and a natural threshold of `n_tests` for "everything right".

### 4. Editor and transfer

- `challenge_editor.py`: `_challenge_fields` takes `score_threshold: str` (a **defaulted** `Form`
  field, empty = `None`, per the htmx rule), parses it as a finite float or returns "The score
  threshold must be a number."; `NEW_CHALLENGE_DEFAULTS["score_threshold"] = None`. Both
  `post_` routes pass it through.
- `_challenge-form.jinja`: a number input (`step="any"`) beside the ranking select; the select
  loses "Average score"; the hint explains: scores are summed raw evaluator scores, and only
  submissions reaching the threshold appear on the leaderboard (on either ranking), the rest are
  marked failed. When the challenge has no threshold (always the case on the create page), an
  `alert-warning` sits under the field: "No score threshold: every graded submission counts as
  passed and goes on the leaderboard, including wrong answers. On the time ranking the fastest
  program wins whatever it printed. With the bundled evaluators, set it to the number of test
  cases to require every test right." It's rendered from the saved value, with no JavaScript, so it
  goes away once a threshold is saved.
- `transfer.py`: optional `score_threshold` key in `challenge.toml` (absent = none; a number, or
  `_require`-style error), written on export only when set. An imported `ranking = "average_score"`
  is **refused** with the usual "Unknown ranking" message listing the known ones; a pushed file is
  one edit away from fixing, and silently mapping it would hide that the meaning changed.
- `cli.py`: nothing unless it formats scores; check `pull` round-trips the new key.
- Seed (`web/seed.py`): give one seeded challenge a threshold, so a dev database shows both a
  cleared and a failed badge out of the box.

### 5. Templates

- `templating.py`: `render_score` as in *Decisions*; docstring.
- `_score-badge.jinja`: context becomes `score`, `cleared`, `has_threshold` (plus the tooltip's
  `n_passed`/`n_tests`); colour from `cleared`; text `"{{ score | score }}"`, prefixed with
  "failed ·" when not cleared. Callers (`f-submissions`, `submission`, `f-standings`) pass the new
  fields.
- `f-leaderboard.jinja`: `total_score` row shows `{{ entry.total_score | score }}` with no
  denominator; drop the `average_score` branch; empty-state text for a thresholded board: "Nobody
  has reached N points yet — be the first."
- `challenge-staff.jinja`: "Best score" header unchanged; mention the threshold next to it when
  set. When it isn't set, the same warning as the editor's, linking to the editor, above the
  standings.
- `_statement.jinja`: show the threshold to students ("Reach N points to make the leaderboard"),
  so "failed" never comes as a surprise.
- `npm run build:css` if any new class names appear.

### 6. Tests

- `test_repository.py`: sums not means; `cleared` with and without a threshold, at the boundary
  (`==` clears); leaderboard excludes failed submissions on both boards; the time board admits a
  cleared submission with a non-`success` outcome and refuses a failed one whose outcomes are all
  `success`; a student whose only
  cleared attempt is older than a failed higher-scoring one is represented by the cleared one;
  standings order cleared before failed; `_clears_threshold` behavioural test.
- `test_challenge_routes.py`: badges no longer say "/ 100"; a failed submission renders "failed";
  editor accepts empty / number / rejects garbage with a 200 alert; the no-threshold warning shows
  on the create page, the edit page and the staff view of a challenge without one, and not when one
  is set.
- `test_transfer.py` / `test_api_routes.py` / `test_cli.py`: threshold round-trips; absent key is
  `None`; `average_score` is refused.
- Migration: upgrade on a database with an `average_score` challenge, check it reads back as
  `total_score` with a `NULL` threshold.

### 7. Docs

- `.claude/rules/domain-rules.md`: replace "Scores are per test case, in `[0, 1]`; the headline is
  their mean ×100" and the leaderboard bullet with the raw-sum / threshold / failed rules, including
  "the judge never learns about thresholds".
- `.claude/rules/judge.md`: the evaluator contract's `score` is raw and summed.
- `CLAUDE.md`: the `Challenge.ranking` sentence gains the threshold (presentation-only, like the
  ranking).
- `DESIGN.md` line ~115 if it describes the 0–100 score.
- The exercise-set skill is untouched: exercises share nothing with challenge scoring.

## Settled with the user

- Failed submissions never appear on the leaderboard, on either ranking.
- On the time ranking the threshold **replaces** `all_passed`: clearing it is the only way onto the board.
- New challenges have no threshold by default, with a clear warning to the teacher in the editor and on
  the staff view.

## Implementation summary (2026-11-02)

All seven steps are implemented and uncommitted. The full suite passes: `uv run pytest` gives 1128
passed, integration tests included.

**1. Schema**
- `model.py` adds `challenge.score_threshold`, a nullable `Float`. `LeaderboardRanking` is now
  `running_time` | `total_score`.
- `schema.py` adds `Challenge.score_threshold: float | None = None` and rewords the
  `EvaluatorResult` docstring.
- Migration `dc71a9e1bcfe_raw_scores_and_score_threshold.py`:
  - Upgrade adds the column, moves `average_score` to `total_score` and rebuilds the
    `leaderboardranking` enum. The default is dropped first and set again afterwards.
  - Downgrade is the reverse. A comment says `average_score` rows can't be recovered.
- `test_migrations.py` covers the `average_score` → `total_score` upgrade.

**2. Queries**
- `rules._clears_threshold(score, threshold)` holds the rule.
- `_score_columns(outcomes, threshold=None)` sums the per-test scores. Given a threshold it also
  returns `score_threshold`, `has_threshold` and `cleared`.
- `cleared` is always false for an ungraded submission (`score IS NOT NULL AND …`).
- The three `text()` queries spell the rule again and say so in a comment.
- `query_user_submissions` orders `is_best` by `cleared DESC, score DESC, attempt DESC`.
- Standings:
  - A `th` CTE holds the threshold.
  - Best attempt is ordered `cleared, score, time`, and the class the same way.
  - Every row carries `has_threshold`.
- Leaderboard:
  - `all_passed` and `average_score` are gone.
  - A `scored` CTE computes `cleared`.
  - Time board: `cleared AND elapsed_s IS NOT NULL`. Score board: `cleared`.
- New `query_challenge_score_threshold` feeds the leaderboard's empty state. An empty board has no
  row to carry the threshold.
- `insert_challenge(..., score_threshold=None)`.
- The plan's "older cleared attempt vs. newer failed **higher-scoring** one" can't happen: the
  threshold is fixed per challenge, so a higher sum always clears if a lower one does.
  - The case that matters is a newer, *faster* failed attempt on the time board. That is what the
    tests cover.
  - `cleared DESC` is redundant when ordering by score, but harmless.

**3. Judge**
- The logged headline score in `run_from_queue` is the sum.
- The docstrings in `evaluators/__init__.py` and the four bundled evaluators say the score is raw
  and summed. The bundled evaluators score each test in [0, 1], so their maximum, and natural
  threshold, is `n_tests`.

**4. Editor and transfer**
- `_challenge_fields` takes `score_threshold` as text from the form or a number from the API.
  - Empty means none.
  - Anything that is not a finite float gives "The score threshold must be a number."; `nan` and
    `inf` are refused.
  - Both `post_` routes take it as a defaulted `Form` field.
- `_challenge-form.jinja`:
  - Adds the threshold input and a new hint.
  - Drops "Average score".
  - Shows the `alert-warning` when no threshold is saved.
- `transfer.Manifest.score_threshold`:
  - Optional; written only when set.
  - A number, not a bool, and finite.
  - An int comes back as a float, so `3` exports as `3.0`.
- `api.py` passes it on push and export. `average_score` is refused by the existing "Unknown
  ranking" path.
- Seed: "Shortest Route Planner" has `score_threshold = 1.0`, so a fresh database shows both
  cleared and failed submissions.

**5. Templates**
- `render_score` prints raw numbers: `7`, `7.5`, `0.33`, `-1.5`. `None` prints as `—`.
- `_score-badge.jinja` is green when cleared, red with "failed · N" when below the threshold, and
  neutral (`badge-ghost`) when there is no threshold. There is no "/ 100".
- The callers pass `cleared` / `has_threshold`: `f-submissions`, `submission`, `f-standings`.
- `f-leaderboard.jinja` shows the raw total. With a threshold, its empty state says "Nobody has
  reached N points yet — be the first."
- `challenge.jinja` loses its `average_score` branches.
- `_statement.jinja` says "Reach N points to make the leaderboard."
- `challenge-staff.jinja` shows "threshold N" under "Best score". With no threshold it shows the
  editor's warning, with a link to the editor.
- No new CSS was needed; the rebuilt `styles.css` is unchanged.

**6. Tests** were written by each step for its own area:
- `test_migrations.py`
- `test_repository.py`
- `test_challenge_routes.py`
- `test_transfer.py`
- `test_api_routes.py`

There is no separate CLI test: `cli.py` is unchanged, and the API test covers a pull, push and pull
again.

**7. Docs**
- `domain-rules.md`: the scores, threshold and leaderboard rules.
- `judge.md`: the evaluator contract.
- `transfer.md`: the `score_threshold` key.
- `CLAUDE.md`: the data-model sentence.
- `DESIGN.md` and `pages.md` needed no change.

**Left as is:** the leaderboard fragment makes two small reads per poll, ranking and threshold.
They could be folded into one.
