---
status: done
---

# Challenge editor: every card saves itself, visibility is an action

## The problem

`/courses/{course_id}/{challenge_id}/edit` mixes two save models with nothing on screen telling them
apart:

- **Problem statement, Leaderboard, Visibility and Sandbox limits** are one `<form>`
  (`_challenge-form.jinja`) posting to `post_challenge_edit`. Its only Save button, and the
  `#edit-result` alert, sit at the bottom of the *Sandbox limits* card. Changing the threshold or
  publishing the challenge means clicking a button in a card about isolate.
- **Evaluator and Test cases** save the moment you act (`#evaluator`, `#testcases`).

All the cards look alike. Edit the statement, scroll down, upload a test case, and it looks as if
everything was saved, but the statement edit wasn't.

## The design

1. **Every card on the edit page saves itself.** Statement, Leaderboard and Sandbox limits each
   become their own form, with a Save button and a status line in their own footer, posting to their
   own route. The rule the evaluator and test-case cards already follow becomes the page's rule:
   *what you change in a card is saved by that card*.
2. **Visibility is an action, not a field.** The three radios become a status line plus buttons:
   **Publish now**, **Schedule…** (a datetime box and a button), **Hide**. They are styled like
   Archive in the danger zone, because they are the same kind of thing: a change of state you make
   on purpose, never as a side effect of saving something else.
3. **The create page keeps one form, and loses Visibility.** A new challenge is always hidden (it has
   no test cases and no evaluator yet), and the editor where the form lands is where you publish.

```
┌ Hidden — only course staff can see it ───────────────────────────┐
│  [Publish now]   Schedule: [2026-11-10 09:00] [Schedule]          │
└──────────────────────────────────────────────────────────────────┘
┌ Problem statement ─────────────────────────────────── markdown ┐
│ [source]                 │ [preview]                           │
│                                   ● unsaved changes   [Save]   │
└────────────────────────────────────────────────────────────────┘
┌ Leaderboard ───────────────────────────────────────────────────┐
│ Rank by [▾]   Score threshold [ 10 ]                           │
│                                   ✓ Saved             [Save]   │
└────────────────────────────────────────────────────────────────┘
┌ Sandbox limits ┐  ┌ Evaluator ┐  ┌ Test cases ┐  ┌ Danger zone ┐
```

## Decisions

- **Card order:** Visibility (now a status strip at the top, under the heading), Problem statement,
  Leaderboard, Sandbox limits, Evaluator, Test cases, Danger zone. Whether students can see the
  challenge is the first thing you want to know when you open the editor.
- **Routes.** Every route depends on `ChallengeEditor` and refuses with 200 + an alert, as now:

  | Route | Form fields | Writes | Answers with |
  |---|---|---|---|
  | `POST …/{challenge_id}/edit/statement` | `description` | `description` | `f-edit-result.jinja` into `#statement-result` |
  | `POST …/{challenge_id}/edit/leaderboard` | `ranking`, `score_threshold=""` | `ranking`, `score_threshold` | `f-challenge-leaderboard.jinja` (whole card, alert inside) |
  | `POST …/{challenge_id}/edit/limits` | `max_time_s`, `max_memory_mb`, `max_threads` | the three limits | `f-challenge-limits.jinja` (whole card, alert inside) |
  | `POST …/{challenge_id}/publish` | none | `visible_from` | `f-challenge-visibility.jinja` |
  | `POST …/{challenge_id}/schedule` | `visible_at=""` | `visible_from` | `f-challenge-visibility.jinja` |
  | `POST …/{challenge_id}/hide` | none | `visible_from = NULL` | `f-challenge-visibility.jinja` |

  `post_challenge_edit` (`POST …/edit`) is removed; `get_challenge_edit` stays. All the new paths
  have three or more segments, so `get_challenge` can't swallow them; no `include_router` reordering
  is needed (`test_no_route_is_shadowed_by_an_earlier_one` will confirm).
- **A card route writes only its own columns.** That also stops two tabs open on the same editor from
  overwriting each other's unrelated edits, which the single form did.
- **Leaderboard, limits and visibility answer with their whole card** (`hx-swap="outerHTML"`), the
  way `#evaluator` does. The card is re-rendered from the database, so the "No score threshold"
  warning comes and goes on save, not on the next page load, and the visibility status line is
  always true.
- **The statement answers with only its status line.** It's the one card where someone may still be
  typing while the request is in flight. Swapping the textarea would drop those keystrokes and lose
  the focus, and there is nothing derived to re-render. A comment in the template says why it's the
  exception.
- **Publish now on a challenge that is already live keeps its date.** The button isn't shown then,
  but a stale tab can still post it. This is `_visible_from_choice`'s rule ("an edit is not a
  republication"), moved into `post_challenge_publish`. Publishing a *scheduled* challenge brings it
  forward to now, as today.
- **Schedule accepts any valid moment, past ones included**, the same as `cocco push`. A past moment
  simply means it is visible now. An empty box → alert "Pick the date and time the challenge
  opens.", the existing message.
- **Hide asks for confirmation only when the challenge is live** (`hx-confirm`, like test-case
  delete), because that is when it takes something off students' pages. Hiding a scheduled
  challenge needs no confirmation.
- **Which buttons each state shows:**

  | State | Status line | Buttons |
  |---|---|---|
  | hidden (`visible_from IS NULL`) | "Hidden: only course staff can see it." | Publish now · Schedule |
  | scheduled (future) | "Opens on {date}. Until then the class sees the title and when it opens." | Publish now · Schedule (reschedule, box pre-filled) · Hide |
  | live | "Visible to the class since {date}." | Hide |

  The state comes from `query_challenge_visibility` (`is_visible` on the database's clock), never
  from comparing dates in the template.
- **Validation is split, not duplicated.** `_challenge_fields` is decomposed into
  `_limit_fields(max_time_s, max_memory_mb, max_threads)`,
  `_leaderboard_fields(ranking, score_threshold)` and `_visible_from_value(visible_from)` (the
  str/datetime/None normalization and `_as_aware`). Each returns `(dict | None, error | None)`.
  `_challenge_fields` stays, with **the same signature**, as their composition, so
  `api_import_challenge` and `post_challenge_new` don't change. The card routes call the piece they
  need. `_visible_from_choice` and `VISIBILITY_CHOICES` go away: the radios were their only caller.
- **Re-rendering a card** reads `query_challenge_config` (plain row) for leaderboard and limits, plus
  `query_challenge_visibility` for the visibility card. No new repository function is needed. The
  card templates read `challenge.<column>` the way `_challenge-form.jinja` does now, so the create
  page's `NEW_CHALLENGE_DEFAULTS` dict still fits them.
- **Unsaved-changes indicator.** One small inline script in `challenge-edit.jinja`, delegated on the
  page:
  - an `input` / `change` event inside a `form[data-card]` sets `data-dirty` on it, which shows a
    "● unsaved changes" hint in its footer;
  - a whole-card swap brings fresh, clean markup, so nothing has to be reset;
  - for the statement, an `htmx:afterRequest` whose swapped alert is `alert-success` clears the
    flag;
  - `beforeunload` warns while any card is dirty.

  The Save button stays enabled throughout. Disabling it until something changes breaks under
  back/forward cache and autofill, and the hint already carries the information. No class name is
  assembled at runtime: the hint is a fixed element shown with a `group-data-[dirty]:` variant
  written out in the template.

## Steps

### 1. Split the validation (`cocco/web/challenge_editor.py`)

- Extract `_limit_fields`, `_leaderboard_fields`, `_visible_from_value` from `_challenge_fields`,
  with the error messages unchanged word for word (tests match on them). Rebuild
  `_challenge_fields` on top of them with the same signature and return shape.
- Remove `_visible_from_choice` and `VISIBILITY_CHOICES`.
- `post_challenge_new`: drop the `visibility` / `visible_at` parameters and pass
  `visible_from=None`.
- Run `uv run pytest cocco/test_api_routes.py cocco/test_transfer.py`: the push path must not
  notice.

### 2. Templates

- Split `_challenge-form.jinja` into three card partials, `_challenge-statement.jinja`,
  `_challenge-leaderboard.jinja` and `_challenge-limits.jinja`. Each renders its card (heading,
  blurb, fields), keeping the existing comments. When a `card_url` is set, a card wraps itself in
  its own `<form data-card hx-post=card_url …>` with a footer (dirty hint, Save button, status
  slot). Unset, it renders bare fields for an enclosing form.
- `challenge-new.jinja`: one `<form hx-post=post_challenge_new>` around the Title card and the
  three partials (no `card_url`), with one submit and `#edit-result` at the end. No Visibility
  card. Update its blurb to say the challenge starts hidden and is published from the editor.
- `f-challenge-leaderboard.jinja`, `f-challenge-limits.jinja`: set `card_url`, include the partial,
  and render the optional `message` / `ok` alert in the footer slot. These are what the routes
  return and what the edit page includes.
- `f-challenge-visibility.jinja`: the status strip and the buttons per the state table, each button
  its own small form targeting the strip with `hx-swap="outerHTML"`, plus the optional alert. Keep
  the datetime box's server-clock comment (`| datetime_local`).
- `challenge-edit.jinja`: the new card order; the statement card included with `card_url` and its
  own `#statement-result`; the dirty-tracking script. Delete `_challenge-form.jinja`.
- `f-edit-result.jinja` is unchanged; its header comment now names both places it lands.

### 3. Routes (`cocco/web/challenge_editor.py`)

- Add the six routes in the table. A small `_render_card(request, name, challenge, message=None,
  ok=False)` helper builds the card responses, so a validation failure re-renders the card **with
  the posted values** and the alert. A refused save must not throw away what the teacher typed:
  pass the posted values over the stored row for that render only.
- Remove `post_challenge_edit`.
- `get_challenge_edit` additionally passes what the visibility strip needs (it already has
  `challenge.visible_from` / `is_visible` from `query_challenge`; check and reuse).

### 4. Tests (`cocco/test_challenge_routes.py`)

Rewrite the ones that post to `…/edit`:

- `test_the_form_publishes_and_schedules` → publish / publish again (date kept) / schedule 2099 /
  publish (brought forward) / hide, through the new routes.
- `test_scheduling_without_a_date_is_an_alert` → `…/schedule` with a blank box.
- The threshold tests (`test_the_editor_saves_a_score_threshold`,
  `test_a_garbage_threshold_comes_back_as_an_alert`,
  `test_the_threshold_is_shown_on_re_edit_and_the_warning_goes`) → `…/edit/leaderboard`. Add: the
  warning disappears **in the save response itself**.

Add:

- Each card route writes only its own columns: seed distinct values, post one card, assert the
  others are untouched.
- Garbage limits (`0`, `-1`) on `…/edit/limits` → 200 alert, nothing written, and the posted values
  are echoed back in the card.
- Every new route is behind `ChallengeEditor`: parametrized over the six, a student gets 403 and a
  challenge under another course gets 404 (mirroring the existing `…/edit` cases around line 328).
- The create page has no `name="visibility"` and still creates a hidden challenge (extend
  `test_a_new_challenge_starts_hidden`).
- The edit page shows "Publish now" for a hidden challenge, only "Hide" for a live one, and has no
  form posting to the old `…/edit` route.

Then `uv run pytest -m "not integration"`.

### 5. Docs

- `.claude/rules/pages.md`: add a **Challenge editor** paragraph. It says each card saves itself;
  leaderboard, limits and visibility answer with their card, the statement with a status line, and
  why; visibility is Publish / Schedule / Hide; the create page has no visibility.
- `.claude/rules/domain-rules.md`, the "Visible" bullet: new challenges are hidden and published
  only from the editor (or `cocco push`).
- `CLAUDE.md`'s htmx rules list `/admin`, people and profile as "one swap target" pages. The editor
  is deliberately per-card, like its evaluator and test cases already were, so no change there; the
  pages.md paragraph covers it.

### 6. Check in the browser

`just web`, open a challenge's editor and check: each card saves on its own with its status in its
own footer; the dirty hint appears and clears; leaving with unsaved edits warns; publish / schedule
/ hide move through the three states; a bad threshold keeps the typed value; the create page still
creates a hidden challenge and lands on the editor. Run `npm run build:css` if a new utility class
(e.g. the `group-data-[dirty]:` variant) was added.

## Out of scope

- Autosave, and drafts of the statement: Design C, rejected for now.
- Tabs: Design B. The cards here fit into tabs later without rework.
- Any schema change: none is needed.

## Outcome (2026-11-02)

Implemented as planned, one step at a time. `uv run pytest -m "not integration"`: 1079 passed.

- **Validation** (`challenge_editor.py`): `_limit_fields`, `_leaderboard_fields`, `_visible_from_value`;
  `_challenge_fields` composes them with the same signature. `_visible_from_choice`,
  `VISIBILITY_CHOICES` and `post_challenge_edit` are gone.
- **Routes**: the six in the table, each writing only its columns via `update_challenge`. Publish
  writes Postgres `func.now()` (not `datetime.now()`) unless `query_challenge_visibility` says the
  challenge is already live. Success messages: "Saved.", "Published.", "Scheduled.", "Hidden.".
- **Templates**: `_challenge-statement/-leaderboard/-limits.jinja` plus one not in the plan,
  `_card-footer.jinja` (the shared hint / status slot / Save button). `f-challenge-leaderboard`,
  `-limits`, `-visibility.jinja`. `_challenge-form.jinja` deleted. A refused leaderboard/limits
  save renders the card with `data-dirty`, so the echoed, unsaved values still show the hint.
- **Tests**: the `…/edit` tests rewritten onto the new routes; added: per-card column isolation,
  garbage limits echoed, the six routes 403/404 for students/other courses, create page has no
  visibility, the strip's buttons per state (and `hx-confirm` only when live).
- **Docs**: pages.md "Challenge editor" paragraph, domain-rules.md "Visible" bullet, and stale
  "the edit form" comments in `course-edit.jinja`, `api.py`, `transfer.py`, `transfer.md`.
- **Step 6**: the Chrome extension wasn't connected, so the check ran over HTTP against a live
  `cocco-web` on the dev database (dummy auth, throwaway challenge created then deleted). Every
  flow behaved as specified. **Not checked in a real browser:** the dirty-tracking script (hint
  appears/clears, `beforeunload`) and the layout.
- Known nits, left as is: a refused limits save echoes `0` as `0.0` (FastAPI parsed the float); a
  refused schedule re-renders the box from the DB (a datetime-local box only posts valid or blank).
