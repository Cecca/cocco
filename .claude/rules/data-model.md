---
paths:
  - "cocco/model.py"
  - "cocco/schema.py"
  - "cocco/repository/**"
  - "cocco/migrations/**"
---

# Data model details

Moved from `CLAUDE.md`'s *Data model* section.

`User.blocked_at` / `blocked_reason` are the **blacklist**, columns on the row (a block always names an existing account), so `get_user` sees it on every request and a block bites on the next click. `_USER_COLUMNS` in `cocco/repository/users.py` is the single column list behind `query_user` and `query_user_by_email`, so no door can forget `blocked_at`.

**Every timestamp is `timestamptz` written by Postgres** (`server_default=func.now()`), never `datetime.now()`: web and judge have two clocks, `now()` has one (the transaction's). `created_at()` / `updated_at()` at the top of `model.py` are factories (a `Column` belongs to one `Table`): both on `user`, `course`, `enrollment`, `challenge`, `exercise_set`; `created_at` alone on `administrator`, `testcase`, `exercise_set_version`; `updated_at` alone on `exercise_session` (created = `opened_at`). `updated_at` is a blanket `onupdate` meaning only "row changed". `set_enrollment_role` sets it by hand, because `onupdate` doesn't fire for an upsert's DO UPDATE.

- **`user.first_login_at` ≠ `created_at`**: `get_or_create_user` also runs from the people page (rosters before term), so NULL means "never signed in". `record_sign_in` sets it once via `coalesce`, alongside `last_login_at`, from `_complete_sign_in` *after* the blacklist check.
- **`submission.started_at` / `finished_at`** bracket a judge run (`pull_queued_submission`, `mark_submission_done`) and `regrade_challenge` resets them to NULL. `submission.enqueued` is that table's `created_at`.
- Neither goes through `_USER_COLUMNS`, which stays the list `schema.User` is built from.

`cocco/schema.py` holds the Pydantic mirrors (including the judge's `SubmissionRunData`). `schema.User`'s block fields have **no defaults**, so a query with a trimmed column list fails validation instead of reporting everyone as unblocked.
