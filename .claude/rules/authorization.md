---
paths:
  - "cocco/main.py"
  - "cocco/repository.py"
  - "cocco/auth.py"
  - "cocco/templates/**"
---

# Authorization details

Moved from `CLAUDE.md`'s *Authorization* section. The summary stays there.

**Enrollment gates a course's contents.** Non-members are shut out, not read-only:
- `require_course_member` 403s anyone with no role on the path's course.
- `require_challenge_viewer` adds the `challenge_in_course` cross-check (as `require_challenge_editor` does), so nobody can name their own course in front of another's challenge.
- `require_challenge_member` is the challenge-only-URL variant (`/f/submissions/{id}`, `/f/leaderboard/{id}`, sample download); with one id there's nothing to disagree with.
- Staff of one course are strangers on every other.

**Outside the gate**, deliberately: the home page and `/f/courses` (or nobody could find a course), `post_course_enroll` (the way in), and `get_course`, which for a non-member renders a **preview** (title, description, `Enroll!`) and branches *before* `query_course_info` runs.

**Two doors into a course**: `post_course_enroll` (self, role fixed to student) and the people page (staff, any role, can remove). `set_enrollment_role` is the write for the latter; `insert_enrollment` survives only for `post_course_new`.

**Refusals are pages.** `Forbidden` is an `HTTPException` subclass carrying the sentence; `handle_forbidden` (registered on that subclass alone) renders `403.jinja`. Handlers can't take dependencies, so `get_user` stashes `request.state.user` for the navbar. Polled fragments are unaffected (htmx ignores error bodies).

**Site administrators** are rows in `administrator`, keyed on *email* so they can be named before first sign-in. `is_administrator` is the single read; `require_admin` (`Admin`) guards course creation, `/admin` and the course danger zone, and its message names the permission ("Only a site administrator can do that."). A course's creator is also enrolled as `teacher`. `[auth] administrators` is a **bootstrap**: `create_db_and_tables` replays it each boot with `ON CONFLICT DO NOTHING`. It only adds; removing a line demotes nobody.

Appointing takes an address; `/admin` and the people page are the **only two** places a human types one, and both call `normalize_email` and refuse addresses outside `allowed_domains` (`_load_auth_config` only *warns* for the config file, so a typo can't stop boot). **There is no revoke** (it's a `DELETE`), so two guards keep an unremovable, unusable admin from existing: **an administrator is never blocked** (in `block_user`'s `WHERE`, race-free) and **a blocked address is never appointed**. This also protects the seeded dev admin. Escape hatches: `DELETE FROM administrator WHERE email = '…';` and `UPDATE "user" SET blocked_at = NULL WHERE email = '…';`

**Blocks are enforced in `get_user`** and again in `_complete_sign_in` (shared by every sign-in path, dummy auth included), so a blocked person sees a sentence instead of a session that dies on the first click. `Blocked` subclasses `Forbidden`; `handle_blocked` renders `blocked.jinja` with **no viewer** (the navbar avatar is behind `CurrentUser` and would 403), which is why `get_user` refuses *before* stashing `request.state.user`. The session is **not** cleared, and polled fragments just 403. It is deliberately not the `HX-Redirect` that `handle_not_authenticated` sends, which would look like being signed out.

**Administrators are staff on every course**, in exactly one place: `effective_course_role` / `effective_challenge_role` return the enrollment's role, or `teacher` for an admin (only looked up when there is no enrollment). So `EDITOR_ROLES` stays per-course and every `role in EDITOR_ROLES` keeps working. Consequences: `get_course` gives admins the course, not the preview, and `get_challenge` takes `can_edit` from the dependency, not from `query_challenge`'s `viewer_role` (a `LEFT JOIN`, NULL for admins).

**Nothing is reachable without an identity**: every route depends on `CurrentUser` or `ApiUser`, directly or via `require_challenge_editor` / `require_api_challenge_editor`. The only open paths are `/login`, `/auth/{provider}`, `/auth/{provider}/callback`, `/static`. `ApiUser` is the only non-session identity (bearer tokens for the CLI; see `transfer.md`).

- **An anonymous htmx request gets `HX-Redirect`, not a 302.** `get_user` raises `NotAuthenticated` (a dependency can't return a `Response`), and the handler branches on `HX-Request`. A 302 would be followed inside the XHR and splice the login page into every polling div.

- **A `next` destination is filtered, never trusted.** `safe_next_path` keeps only an on-site path (one leading `/`, no scheme or authority), applied both before stashing and after reading back. The OAuth redirect URI carries no destination (providers match it exactly), so `next` travels in the session; without one the callback lands on `/`.
- **CSRF rests entirely on `same_site="lax"`**: there's no CSRF token anywhere, and `POST /logout` is a form so an `<img>` can't fire it. Changing `same_site` silently removes the defence.
