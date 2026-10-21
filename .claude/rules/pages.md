---
paths:
  - "cocco/main.py"
  - "cocco/templates/**"
  - "cocco/repository.py"
---

# Pages and their routes

Moved from `CLAUDE.md`'s *HTTP layer* section. The htmx rules and route order stay there.

**Challenge and submission pages.** One URL, two pages: `get_challenge` renders `challenge-staff.jinja` for staff (with `query_challenge_standings`, linking to `get_challenge_student`) and `challenge.jinja` otherwise; `?as=student` shows staff the student view. `get_submission` is flat at `/submissions/{id}`, since the id determines the challenge. It shows Pygments-highlighted source, compiler output and per-test outcomes. The last two are the polled `f-submission-results.jinja` using `query_submission_outcomes` (not `query_submission`), so the source isn't re-sent every poll. Both the student history and the staff per-student view list submissions via the same `f-submissions.jinja`.

Uploads carry no language field: `post_submission` maps the extension through `EXTENSION_TO_LANGUAGE`, built from `languages.toml`.

**Home page** (`index`, `index-logged-in.jinja`): enrolled courses, then everything else (`query_courses` splits on `role IS NULL`), both as `_course-card.jinja`, the deliberate twin of `_challenge-card.jinja`. The filter `q` matches the **title** with `position()`, not `ILIKE`, so `%` and `_` are literal with no escaping. It swaps `#courses` with `f-courses.jinja` (both halves), which the page also inlines on first paint.

**Enrolling yourself** is `post_course_enroll`. On a home-page card it re-renders the courses fragment (with `q`) so the course moves to the top half. On the course preview it sends `next` (through `safe_next_path`) and gets an `HX-Redirect` back. Its authorization is `CurrentUser` only: it is **the only route under a course a non-member may call**. So the role is fixed inside `insert_self_enrollment` (no form field can spell `teacher`), and its `ON CONFLICT DO NOTHING` makes double clicks no-ops and never demotes an existing teacher. There is no self-unenroll.

**`course.enrollment_key`**: NULL = anyone signed in may join; otherwise `post_course_enroll` compares it with `hmac.compare_digest` on the trimmed string before writing. It's stored and shown **in plaintext** on purpose (staff read it out to the class; it guards a class list, not an account), so the **course editor is the only place it's printed**. `query_courses` / `query_course` expose only `requires_key` (`enrollment_key IS NOT NULL`) and list columns rather than `SELECT *`; `query_enrollment_key` is the single read. The key field lives only on the course preview (`f-enroll.jinja`, re-swapped with the message on a bad key); a keyed course's card shows an `Enroll…` link to it.

**Course creation** is `/courses/new` (`course-new.jinja`), button shown only to administrators; on success → the new course's editor.

**`/admin`** (`admin.jinja`, behind `Admin`) lists users, appoints administrators and blocks accounts. Blocking is an inline per-row form with a reason, not a `<dialog>`: it's reversible, and N dialogs would be rebuilt on every filter keystroke.

**Course editor** `/courses/{course_id}/edit`: title, description (same markdown textarea and `post_markdown_preview` as challenges), and the challenge list reordered with ↑/↓. Cards are `_challenge-card.jinja`, shared with the course page via `f-course-challenges.jinja`, so the two lists can't drift.

**Danger zones** end both editors: archive (reversible) and delete (not).
- The course's is **`Admin` only**, not `CourseEditor`: what exists site-wide is the site's business. `get_course_edit` passes `is_admin` to draw it; the routes check for themselves.
- The challenge's is behind `ChallengeEditor` (admins + staff, with the `challenge_in_course` cross-check).
- Archive/unarchive are two routes (like `/block` / `/unblock`). All six answer `HX-Redirect`: an archive goes back to the same editor, a deleted challenge to the course editor, a deleted course to the home page.
- **A delete asks for the title, typed out**: a `<dialog class="modal">` whose inline script only *enables* the button on a match. `_confirmation_matches` re-checks in the route, because a disabled button is presentation. It uses a plain `==` on the trimmed string: the title is on the page, so this confirms intent rather than proving a secret. The title reaches the script via **`data-expected`** (Jinja attribute escaping), never a JS string literal (`Dijkstra's` would break it).
- Both refuse with 200 + `f-edit-result.jinja` on a wrong name or while a judge is mid-run.

**Archiving is visibility, not a lock**, with exactly **three enforcement points**: `post_submission`, `post_course_enroll`, and the exercise Run/Check routes (`_post_session_job`, `_post_exercise_preview_job`). Exercise *builds* are allowed, because importing is editing. An archived course or challenge stays readable to those enrolled, stops taking submissions, leaves other people's listings and admits nobody new. Staff keep editing it (via the web or `cocco push`) and unarchive it from the same place.

**People page** `/courses/{course_id}/people` (`course-people.jinja`): the roster, roles, submission counts here, and staff's three mutations. It is behind `CourseEditor` for reads too, because a roster lists real people. Enrolling takes an **address** (rosters predate sign-in): `enrollment` is keyed on `user.id`, so the account is **created** via `get_or_create_user`, the same call the OAuth callback uses. So pre-enrolled people are ordinary `user` rows on `/admin`. A *blocked* account is **not** refused here (unlike appointing an admin), because a block stops a person without retracting their place on a course.
- **You cannot remove yourself** (checked in the route).
- **A course never loses its last teacher**: checked inside `set_enrollment_role`'s `ON CONFLICT` and `delete_enrollment`'s `WHERE`. It is sound only because every mutation takes `lock_course` (`FOR UPDATE` on the course row) first; under READ COMMITTED, two concurrent demotions would otherwise both succeed.
- `Remove` on someone who has submitted asks via `hx-confirm`, not a `<dialog>`, because nothing is destroyed.

**Profile** `/users/{user_id}` (`profile.jinja`): one URL, two pages (forms for the owner, text for an administrator).
- `require_profile_reader` allows owner or admin and **returns `can_edit`** (like `authorize_submission`'s `is_staff`). `require_profile_owner` has no admin branch: reading is oversight, renaming is impersonation.
- Course staff are strangers here, because the page spans every course.
- The address is editable by nobody: it's the identity a provider proves.
- `update_user` is the write; blocks go through `block_user`, whose `WHERE` carries a rule `update_user` would lose.
- Mutations answer `#identity` from `query_user` (plain Core). The address, badges and course list sit outside it, keeping the Postgres-only `query_user_profile` out of the mutation path.
- A rename also swaps `_navbar-identity.jinja` **out of band**, so the navbar can't go stale.
- **Avatar** is `user.image`, a blob whose type comes from `sniff_image_mime`, **never** the upload's `content_type`, because `user_avatar` serves the bytes from this origin. SVG is refused (we generate identicons, not uploads). The response has `nosniff`, a `default-src 'none'` CSP and `Cache-Control: no-cache` (the URL doesn't change with the picture).
- `Generate from my name` reseeds `generate_identicon` from the current nickname; a rename never redraws an avatar by itself.

- **Source is highlighted server-side**: `render_source` (the `highlight` filter) runs Pygments on `submission.language` (every `languages.toml` name is a Pygments alias), falling back to plain text. Token colours are hand-written under `.cocco-code` in `assets/styles.css`.
