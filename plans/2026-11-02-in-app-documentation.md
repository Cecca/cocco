---
status: done
---

# In-app documentation

## The goal

cocco should explain itself, above all to teachers. Every section of a page, and where it helps every box (the
`card`s with an `<h2>` title), gets a **?** button beside its heading. The button opens a pop-up with the docs for
that box, and the pop-up links to the full documentation page the box's text comes from. Today the only help is the
one-line grey subtitle under some headings (e.g. `_challenge-form.jinja`'s Leaderboard and Visibility cards) and
the `/about` page.

Both outcomes the request names (a pop-up, or a dedicated page) come from **one source**. Every box's help is a
section of a documentation page, so writing the docs once gives both views, and the pop-up is never a second copy
that drifts.

## Decisions

### 1. Docs are Markdown in the package, like `about.md`

- Docs live in `cocco/docs/<topic>.md` and ship in the wheel, resolved from `Path(__file__)` (no working-directory
  reads; see `packaging-and-config.md`). Check that `pyproject.toml`'s package data picks up `*.md` under
  `cocco/docs/`. `about.md` and `skills/**/*.md` already ship, so it most likely does.
- They are read **per request**, the way `get_about` reads its file, so an edit shows without a restart. The files
  are small, and a cache keyed on mtime can come later if profiling ever asks for one.
- Each file starts with a small front matter block: `title`, `audience` (`staff`, `student` or `everyone`) and
  `order`. The index page groups and orders the files by these values.
- **A section is a heading with an explicit, stable id**: `## Leaderboard {#leaderboard}`. The ids are written out
  rather than generated from the heading text, so a reworded heading never breaks a ? button that points at it.

### 2. Docs get their own Markdown renderer, not the `markdown` filter

Explicit ids need the `attr_list` extension (plus `toc` for the page's table of contents). The shared
`render_markdown` filter also renders **challenge statements and course descriptions**, which teachers write. Adding
`attr_list` there would let a statement put arbitrary attributes on elements (`{onclick=…}`). So `cocco/web/docs.py`
gets its own `render_doc` with `fenced_code`, `tables`, `codehilite` (same config, so code looks the same),
`attr_list` and `toc`. The `markdown` filter stays as it is.

### 3. Routes: one page, one fragment, one index

New module `cocco/web/docs.py`, included in `main.py` next to `about`:

| Route | Template | What it returns |
|---|---|---|
| `GET /docs` | `docs-index.jinja` | Every topic, grouped by audience, in `order` |
| `GET /docs/{topic}` | `docs.jinja` | A whole file, with a table of contents built from its `##` headings |
| `GET /f/docs/{topic}/{section}` | `f-doc-section.jinja` | One section (from its heading to the next heading of the same or a higher level), plus a "Read the full page →" link to `/docs/{topic}#{section}` |

- **Open to everyone, like `/about`.** Nothing in the docs is secret, a student who is curious about what a teacher
  sees loses nothing by reading it, and an open page is one fewer gate to keep right. The page uses `get_user` with
  the same `NotAuthenticated` / `Blocked` fallback as `get_about`, so the navbar still shows whoever is signed in.
- **`topic` is a slug, never a path.** The route looks it up in the set of files found in `cocco/docs/` and returns
  404 for anything else. It never joins user input onto a directory (the same rule `Storage.resolve` enforces for
  storage).
- An unknown section is a 404 too. The test in step 4 keeps the buttons from ever pointing at one.
- Route order: `/docs` collides with nothing. `/f/docs/...` sits beside the other `/f/` routes.

### 4. The ? button is a macro, the pop-up is one `<dialog>` in `base.jinja`

`cocco/templates/_help.jinja` defines a macro, and pages import it:

```jinja
{% from "_help.jinja" import help %}
<h2 class="font-bold text-lg">Leaderboard {{ help("challenges", "leaderboard") }}</h2>
```

It renders:

```html
<a href="/docs/challenges#leaderboard"
   hx-get="/f/docs/challenges/leaderboard" hx-target="#help-body" hx-swap="innerHTML"
   class="btn btn-ghost btn-circle btn-xs …" aria-label="Help: Leaderboard">?</a>
```

- **It is a real link with htmx on top.** Without JS, or on a middle-click, it opens the full page at the right
  anchor. With htmx it loads the section into the dialog. The class list is fixed text in the macro, never built at
  runtime (Tailwind rule).
- `base.jinja` holds **one** `<dialog id="help_modal" class="modal">` with a `#help-body` div and the backdrop
  close form, the same pattern as the danger-zone dialogs. A small listener opens it after the swap lands
  (`htmx:after:swap` on `#help-body`: check the exact htmx 4 event name, which `exercise.js` writes as
  `htmx:before:swap`). It is one dialog for the whole page, not one per button. The same reasoning as `/admin`'s
  inline block forms applies: buttons inside polled fragments would otherwise rebuild a dialog on every poll.
- The dialog sits **outside every swap target**, so a polled fragment (`f-exercise-session.jinja`,
  `f-standings.jinja`, …) can't swap it away while it is open. A ? button *inside* a polled fragment is fine. It is
  rebuilt with the fragment, and that does no harm.
- The grey one-line subtitles stay: they are the summary, and the ? holds the explanation. Any subtitle that has
  grown into a paragraph (Leaderboard's second `<p>`, Visibility) moves into the docs and is cut down to one line.

### 5. The docs are held to the code by tests, as the exercise skill is

`test_exercise_skill.py` already does this for `SKILL.md`, and the docs get the same treatment in
`cocco/test_docs.py`:

- **Every `help(topic, section)` call in `cocco/templates/**/*.jinja` resolves** to an existing file and heading id.
  The test scans the templates with a regex for `help("…", "…")`. This is the test that matters: it turns a renamed
  section into a test failure instead of a 404 pop-up.
- Every file has valid front matter and renders, and every heading id is unique within its file.
- Every internal link (`/docs/<topic>#<id>`) in the docs resolves.
- Every file appears on `/docs`.
- Facts that drift get the `test_every_language_is_documented` treatment. Every language in `languages.toml` is
  named in `submissions.md`, every `LeaderboardRanking` in `challenges.md#leaderboard`, and every bundled evaluator
  in `evaluators.md`.
- Route tests in the style of `test_about_routes.py`: anonymous read, a signed-in navbar, an unknown topic or
  section is 404, and a path-traversal slug (`..%2Fabout`) is 404.

### 6. Links in the navbar and in context

- The navbar's `About` gets a sibling `Docs` link to `/docs`.
- Empty states link to the right page: a course with no challenges links to "Creating a challenge", and a set with
  no versions links to the import format.
- The exercise import box already offers the downloadable skill (`cocco-exercises`). `exercises.md#format` links
  to that download and **does not repeat or render the format reference**. `SKILL.md` stays the one place the
  format is written down, with its tests, and `/docs` never shows its model-facing wording.

## Content: topics and the boxes that point at them

The ids in the second column are what the `help()` calls name. The list comes from the `<h2>` / `<h3>` headings in
the current templates.

| Topic file (audience) | Sections (`{#id}`) | Where the ? goes |
|---|---|---|
| `getting-started.md` (staff) | `overview` (courses → challenges, exercise sets → sessions), `roles` (teacher, collaborator, student, administrator), `first-course` (a walkthrough) | `/docs` index. Home page heading for staff |
| `courses.md` (staff) | `create`, `title`, `enrollment-key`, `description`, `challenge-order`, `archive`, `delete`, `people`, `enroll-by-address`, `roles`, `last-teacher` | `course-new.jinja`, `course-edit.jinja` cards (Title, Enrollment key, Description, Challenges, Danger zone), `course-people.jinja` heading, `course.jinja` section headings |
| `challenges.md` (staff) | `create`, `statement` (Markdown, code fences, tables, preview), `leaderboard` (the three rankings and when each fits), `visibility` (hidden / live / opens at), `limits` (time, memory, what happens on exceed), `standings`, `regrade`, `student-view`, `archive`, `delete` | `_challenge-form.jinja` cards (Title, Problem statement, Leaderboard, Visibility, Sandbox limits), `challenge-staff.jinja` (Class standings, regrade dialog), `challenge-edit.jinja` (Danger zone) |
| `test-cases.md` (staff) | `add`, `samples` (`is_sample`: downloadable), `active` (`is_active`: graded, and retiring instead of deleting), `order`, `large-files` | `f-testcases.jinja` (list, "Add a test case"), Test cases card in `challenge-edit.jinja` |
| `evaluators.md` (staff) | `default` (no evaluator: exact comparison), `contract` (three paths on argv, one JSON object `{"score", "message"}`, a score in [0, 1]), `bundled`, `upload`, `languages` | `f-evaluator.jinja` (Evaluator card, "Upload an evaluator", "Start from a bundled evaluator") |
| `submissions.md` (everyone) | `languages` (extension → language, compiler versions), `statuses` (queued / running / done, and why one stays queued), `results` (per-test score, time, memory, compiler output), `hidden-outputs` | `challenge.jinja` ("Submit a solution", "Your submissions"), `submission.jinja`, `f-submission-results.jinja` |
| `exercises.md` (staff) | `what` (exercises vs challenges), `format` (links to the skill download), `import`, `versions`, `sessions`, `open-session`, `dashboard`, `projector`, `unlocking` (next / previous, several teachers), `switch-version`, `close`, `preview`, `delete` | `course.jinja` (Exercise sessions, Exercises, Import a set), `exercise-set.jinja` + `f-exercise-set.jinja` (Sessions, Open a session, Import a new version, Latest import, Versions, Danger zone), `f-exercise-session.jinja` (Exercises, Version, Close), `exercise-dashboard.jinja`. **Not** `exercise-projector.jinja` or `f-exercise-projector.jinja`: `projector` is reached from the dashboard's ? |
| `cli.md` (staff) | `tokens`, `install`, `ls-pull-push`, `zip-format` (summary of `transfer.md`'s format, written for the people who use it) | `profile.jinja` API tokens card, `f-tokens.jinja` |
| `account.md` (everyone) | `sign-in` (providers, password), `profile`, `avatar`, `password` | `profile.jinja` cards, `login.jinja` |
| `administration.md` (staff, admin-only content) | `administrators`, `blocking`, `creating-courses`, `course-danger-zone` | `admin.jinja` heading, `f-users.jinja` |
| `students.md` (student) | `enrolling` (keys), `submitting`, `reading-results`, `leaderboard`, `exercises-in-class` | `f-enroll.jinja`, `challenge.jinja`, `exercise.jinja` (Tests, Solution), `exercise-wait.jinja` |

Every section is written for its reader. For teachers that means saying what the setting does to *students*. "A
hidden challenge disappears from the course page, but submissions and scores are kept" is the sentence they need,
and the column name it maps to is not.

## Steps

Each step ends with `uv run pytest -m "not integration"` green and the pages checked in the browser.

1. **Infrastructure.** `cocco/web/docs.py` (loader, front matter, section split, `render_doc`, the three routes),
   `docs.jinja`, `docs-index.jinja`, `f-doc-section.jinja`, `_help.jinja`, the dialog and listener in
   `base.jinja`, the navbar link, styling for the `?` button in `assets/styles.css` if the daisyUI classes are not
   enough, and `test_docs.py` with the route tests and the "every `help()` resolves" test. One placeholder topic
   with one section, wired to one ? button (the challenge Leaderboard card), proves the whole path end to end.
2. **Challenge editor and test cases.** These are the most-used teacher pages and hold the most settings that are
   not obvious: `challenges.md`, `test-cases.md`, `evaluators.md`, with ? buttons on every card of
   `_challenge-form.jinja`, `challenge-edit.jinja`, `f-testcases.jinja`, `f-evaluator.jinja` and
   `challenge-staff.jinja`. Cut the long subtitles down to one line. Add the ranking and evaluator drift tests.
3. **Courses and people.** `getting-started.md` and `courses.md`, with buttons on `course-new`, `course-edit`,
   `course-people` and `course` section headings.
4. **Exercises.** `exercises.md`, with buttons on the set, session and dashboard pages. **No buttons on the
   projector view** (`exercise-projector.jinja`, `f-exercise-projector.jinja`), because the class sees that screen.
   `test_docs.py` asserts that neither template calls `help(`, so a later pass that adds a ? to every box can't put
   one there by accident.
5. **The rest.** `submissions.md` (with the languages drift test), `cli.md`, `account.md`, `administration.md`,
   `students.md`, with their buttons. Add the empty-state links.
6. **Repo docs.** A `.claude/rules/docs.md` guide: where docs live, the `{#id}` rule, "a new card gets a ? and a
   section", the separate renderer and why. Add it to the guide list in `CLAUDE.md`, and add `/docs` to `pages.md`
   and `authorization.md` (open to anonymous visitors, like `/about`).

## Settled questions

- **All docs are public.** `/docs` lists every topic to everyone, including anonymous visitors, grouped by
  audience. Nothing is filtered by role. `audience` only decides the grouping on the index.
- **The exercise format is a link to the skill, not a rendered page.** See decision 6.
- **No ? buttons on the projector view.** See step 4.

## Open questions

- **Language.** The UI is English, and so are the docs. If an Italian version is ever wanted, the topic layout
  (`cocco/docs/<lang>/<topic>.md`) can grow into it. Not in scope here.
- **Screenshots** would help the walkthroughs but go stale with every UI change. Leave them out for now.

## Out of scope

- Search across the docs (the index page plus the browser's find is enough at this size).
- Docs a teacher edits from the web UI: the docs are part of the release, like `about.md`.
- Tooltips on individual form fields. The ? sits at the box level, and a field that needs more than its label and
  placeholder gets a sentence in its box's section.
