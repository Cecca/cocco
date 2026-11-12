---
paths:
  - "cocco/web/**"
  - "cocco/templates/**"
  - "cocco/repository/**"
---

# Pages and their routes

Moved from `CLAUDE.md`'s *HTTP layer* section. The htmx rules and route order stay there.

**Challenge and submission pages.** One URL, two pages: `get_challenge` renders `challenge-staff.jinja` for staff (with `query_challenge_standings`, linking to `get_challenge_student`) and `challenge.jinja` otherwise; `?as=student` shows staff the student view. `get_submission` is flat at `/submissions/{id}`, since the id determines the challenge. It shows Pygments-highlighted source, compiler output and per-test outcomes. The last two are the polled `f-submission-results.jinja` using `query_submission_outcomes` (not `query_submission`), so the source isn't re-sent every poll. Both the student history and the staff per-student view list submissions via the same `f-submissions.jinja`. A reference shows "Reference solution: *label*" instead of its author (staff get a link back to the editor card); who may open it is `authorize_submission`'s reference branch.

Uploads carry no language field: `post_submission` maps the extension through `EXTENSION_TO_LANGUAGE`, built from `languages.toml`.

**Home page** (`index`, `index-logged-in.jinja`): enrolled courses, then everything else (`query_courses` splits on `role IS NULL`), both as `_course-card.jinja`, the deliberate twin of `_challenge-card.jinja`. The filter `q` matches the **title** with `position()`, not `ILIKE`, so `%` and `_` are literal with no escaping. It swaps `#courses` with `f-courses.jinja` (both halves), which the page also inlines on first paint.

**Enrolling yourself** is `post_course_enroll`. On a home-page card it re-renders the courses fragment (with `q`) so the course moves to the top half. On the course preview it sends `next` (through `safe_next_path`) and gets an `HX-Redirect` back. Its authorization is `CurrentUser` only: it is **the only route under a course a non-member may call**. So the role is fixed inside `insert_self_enrollment` (no form field can spell `teacher`), and its `ON CONFLICT DO NOTHING` makes double clicks no-ops and never demotes an existing teacher. There is no self-unenroll.

**`course.enrollment_key`**: NULL = anyone signed in may join; otherwise `post_course_enroll` compares it with `hmac.compare_digest` on the trimmed string before writing. It's stored and shown **in plaintext** on purpose (staff read it out to the class; it guards a class list, not an account), so the **course editor is the only place it's printed**. `query_courses` / `query_course` expose only `requires_key` (`enrollment_key IS NOT NULL`) and list columns rather than `SELECT *`; `query_enrollment_key` is the single read. The key field lives only on the course preview (`f-enroll.jinja`, re-swapped with the message on a bad key); a keyed course's card shows an `Enroll…` link to it.

**About page** `/about` (`about.jinja`, `cocco/web/about.py`) renders `cocco/about.md` through the `markdown` filter. The file is authored in the repo and ships in the wheel; it is read **per request** (an edit shows without a restart, which `--reload` would not catch). Open to anonymous visitors, linked from the navbar on every page.

**Docs** `/docs`, `/docs/{topic}` and the `/f/docs/{topic}/{section}` fragment (`cocco/web/docs.py`) render `cocco/docs/*.md` through their own `render_doc`, not the `markdown` filter. Read per request like `/about`, open to anonymous visitors, linked from the navbar beside `About`; the fragment fills the one `#help_modal` dialog in `base.jinja` for the ? buttons. See `docs.md`.

**Course creation** is `/courses/new` (`course-new.jinja`), button shown only to administrators; on success → the new course's editor.

**`/admin`** (`admin.jinja`, behind `Admin`) lists users, appoints administrators and blocks accounts. Blocking is an inline per-row form with a reason, not a `<dialog>`: it's reversible, and N dialogs would be rebuilt on every filter keystroke.

**Course editor** `/courses/{course_id}/edit`: title, description (same markdown textarea and `post_markdown_preview` as challenges), and the challenge list reordered with ↑/↓. Cards are `_challenge-card.jinja`, shared with the course page via `f-course-challenges.jinja`, so the two lists can't drift.

**Challenge editor** `/courses/{course_id}/{challenge_id}/edit` (`challenge-edit.jinja`, behind `ChallengeEditor`): **each card saves itself**, to its own route, writing only its own columns (so two tabs can't overwrite each other's unrelated edits). Statement, leaderboard and limits are forms with a Save button and status line in their own footer; the evaluator and test cases already worked that way.
- **Leaderboard, limits and visibility answer with their whole card** (`hx-swap="outerHTML"`), re-rendered from the database, so the "no score threshold" warning and the visibility status are always true. A refused save (200 + alert inside the card) echoes the **posted** values and keeps the dirty flag; it never throws away what was typed.
- **The statement answers with only a status line** (`f-edit-result.jinja` into `#statement-result`): someone may still be typing while the request is in flight, and swapping the textarea would drop keystrokes and focus. Nothing derived needs re-rendering.
- **Visibility is an action, not a field**: a status strip with **Publish now**, **Schedule** (a datetime box) and **Hide**. The state comes from `query_challenge_visibility`, never from comparing dates in the template. Publish stamps Postgres `now()`, but keeps a live challenge's date (an edit is not a republication; a stale tab can still post it); publishing a scheduled one brings it forward. Schedule accepts any valid moment, past included, like `cocco push`. Hide asks to confirm only when the challenge is live.
- The unsaved-changes hint and the `beforeunload` warning are one small script in `challenge-edit.jinja`, keyed on `form[data-card]`.
- **The Reference solutions card** (`f-challenge-references.jinja`, routes in `cocco/web/references.py`, all behind `ChallengeEditor`) is not a `form[data-card]`: each control saves itself and every route answers the whole card (`#references`, alert included). It polls itself every 3s while a reference is queued or running; a `htmx:before:request` listener in `challenge-edit.jinja` cancels a poll while a label is half typed or a file chosen (htmx 4 beta2 doesn't parse `every 3s [expr]`, so it isn't an `hx-trigger` filter). The visibility options read **Staff only** / **Leaderboard** / **Leaderboard and source**; a blank label on add defaults to the file name, on rename it is refused. Archived challenges still accept references.
- **The create page** (`challenge-new.jinja`) is one form with one submit and **no visibility**: a new challenge starts hidden and is published from the editor it lands on. It shares the card partials (`_challenge-statement.jinja`, `_challenge-leaderboard.jinja`, `_challenge-limits.jinja`) with the editor; without a `card_url` they render bare fields.

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
