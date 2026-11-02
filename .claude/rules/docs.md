---
paths:
  - "cocco/docs/**"
  - "cocco/web/docs.py"
  - "cocco/templates/**"
  - "cocco/test_docs.py"
---

# Documentation (`cocco/docs/`, the ? buttons)

`plans/2026-11-02-in-app-documentation.md` is the long form.

**The docs are Markdown in the package**: `cocco/docs/<topic>.md`, found by `topic_files()` from `DOCS_DIR` (resolved from `__file__`), shipped in the wheel like `about.md`, and read **per request**, so an edit shows without a restart. Each file opens with a front matter block of `title`, `audience` (`staff`, `student` or `everyone`) and `order`, checked by `parse_doc` (`DocError`). **All docs are public**: `audience` only groups the index and nothing is filtered by role.

**Three routes** in `cocco/web/docs.py`, all open (see `authorization.md`):
- `GET /docs` (`docs-index.jinja`): every topic, grouped by audience, sorted by `order` then title.
- `GET /docs/{topic}` (`docs.jinja`): the whole file, with a table of contents from its `##` headings that carry an id.
- `GET /f/docs/{topic}/{section}` (`f-doc-section.jinja`): one section and a "Read the full page" link. `section_source` cuts from the heading to the next heading of the **same or a higher level**, skipping fenced code.
- `topic` is a slug looked up in `topic_files()`, never joined onto a directory; an unknown topic or section is a 404.

**A section is a heading with an explicit id**: `## Leaderboard {#leaderboard}`. Ids are written out, stable and unique within the file, never derived from the heading text, so rewording a heading breaks no ? button. Renaming an id means renaming every `help()` call and link that names it.

**A new card gets a ? and a section.** The button is the `help(topic, section, label)` macro in `_help.jinja`, placed in the card's heading: `{{ help("challenges", "leaderboard", "Help: Leaderboard") }}`. The label is the `aria-label`. It is a real link to `/docs/{topic}#{section}` with `hx-get` to the fragment on top, so it works without JS and on a middle-click. Its classes are fixed text in the macro.
- The pop-up is **one** `<dialog id="help_modal">` in `base.jinja`, with `#help-body` as every button's `hx-target`, sitting **outside every swap target**. A dialog per button would be rebuilt on every poll of the fragment holding it (`/admin`'s reason for inline block forms), and one inside a polled fragment could be swapped away while open. A button inside a polled fragment is fine.
- The dialog opens from an `htmx:after:swap` listener in `base.jinja` that checks the request's `ctx.sourceElement` against `[data-help]`. Nothing else may carry `data-help`.
- **Guard staff-only buttons on pages students see.** `course.jinja` branches on `can_edit`, pointing staff at `courses` / `exercises` and students at `students.md`. A student's ? should explain what they can do.
- **No ? on the projector views** (`exercise-projector.jinja`, `f-exercise-projector.jinja`): the class sees that screen. `exercises.md#projector` is reached from the dashboard's ?.
- An empty state links with `url_for('get_doc', topic=…) }}#id` rather than a ?.

**The docs have their own renderer, `render_doc`**, with `attr_list` and `toc` on top of the `markdown` filter's extensions (same `codehilite` config). `attr_list` must **never** reach the shared `markdown` filter (`render_markdown` in `templating.py`): it also renders challenge statements and course descriptions, which teachers write, and would let them put arbitrary attributes (`{onclick=…}`) on elements.

**The exercise format is written down only in the skill's `SKILL.md`** (held to the parser by `test_exercise_skill.py`). `exercises.md#format` links to the download (`/skills/cocco-exercises.zip`) and never repeats or renders the reference.

**Writing the docs.**
- Say what a setting does **to students**, not which column it sets: "a hidden challenge disappears from the course page, but submissions and scores are kept".
- Each section stands alone: it is what the pop-up shows, with nothing above it.
- Link rather than repeat (`[…](/docs/<topic>#<id>)`).
- Name controls by their exact UI label, in bold.
- The grey one-line subtitle under a heading stays as the summary; the ? holds the explanation. A subtitle that grows into a paragraph moves into the docs.
- **The code wins.** When a doc disagrees with the code, fix the doc, and let the tests below catch what they can.

**What `cocco/test_docs.py` enforces:**
- Every file has valid front matter and renders; heading ids are unique within a file; every file is listed on `/docs`.
- Every `help("topic", "section")` in `cocco/templates/**/*.jinja` resolves to a file and heading id (a regex scan, with a guard that it finds at least one call).
- Every template link `url_for('get_doc', topic='…') }}#id` resolves, and at least one exists.
- Every internal link `(/docs/<topic>#<id>)` in the docs resolves.
- Drift tests: every `LeaderboardRanking`, by its label in `_challenge-leaderboard.jinja`'s options, is bolded in `challenges.md#leaderboard`; every bundled evaluator (`BUNDLED`) appears as `**title** (`file`)` in `evaluators.md#bundled`; every `languages.toml` extension is in `evaluators.md#languages`; every language's name, extensions, compiler or interpreter and compiler flags are in `submissions.md#languages`.
- The example evaluator in `evaluators.md#contract` runs and honours the `EvaluatorResult` contract.
- Neither projector template contains `help(`.
- `render_doc` emits explicit ids and the `markdown` filter does not.
- The routes: anonymous reads, the signed-in navbar, 404 on an unknown topic or section and on a traversal slug (`..%2Fabout`), and a per-request read.

**FastAPI's Swagger UI is off** (`docs_url=None` in `main.py`), because it answered `/docs`. Turning it back on needs another URL.
