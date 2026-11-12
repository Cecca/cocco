---
paths:
  - "cocco/transfer.py"
  - "cocco/cli.py"
  - "cocco/web/api.py"
  - "cocco/test_transfer.py"
  - "cocco/test_cli.py"
  - "cocco/test_api_routes.py"
---

# The transfer API (`/api`, `cocco/transfer.py`, `cocco/cli.py`)

A challenge is otherwise only reachable one HTTP form at a time: an upload per test case, an
arrow-press per reorder, a `<textarea>` for the statement. **The API makes a challenge a file** —
`cocco pull 12`, edit, `cocco push 12` — and the ergonomic point is that reordering test cases
becomes moving two lines in a text editor and the statement becomes a markdown file you can keep in
git.

The zip is flat and **mirrors what is already on disk**: `challenge.toml`, `README.md` (the
statement, verbatim), `tests/`, `evaluator/` when the challenge has an uploaded checker, and `references/` when it has reference solutions. Test
files live in one directory per challenge with no per-test subdirectory, which is why input and
output names are already unique within a challenge — `tests/` keeps exactly that shape, and that is
why **a test case is identified by its input file name**. The manifest carries no ids on purpose: a
directory is meant to be edited, copied between checkouts and committed, so renaming an input
*replaces* the test case rather than renaming it. `score_threshold` is an optional key (absent = no threshold); `average_score` as a ranking is refused. Three things are deliberately absent from it.
`position` belongs to the course editor, and a key that silently did nothing would be worse than no
key; `course_id` and `challenge_id`, because the challenge a push lands on is the one in the URL,
never a number inside a file somebody may have copied; and `execution_order`, because the block
order *is* the order.

**`cocco/transfer.py` touches nothing** — no FastAPI, no `AsyncConnection`, no `Storage` — which is
the seam that makes the format testable by import, the way `auth.py` keeps the interesting half of
sign-in unit-testable. It owns the shape (`parse_manifest`, `dump_manifest`) and the rules that
must not be got wrong (`plan_testcases`, `plan_references`); `parse_manifest(text, known_extensions=None)` takes the extensions as an argument because `transfer.py` imports no language table, and `None` skips the check (the CLI's fingerprints); `cocco/web/api.py` owns the I/O. Written with `toml.dumps` and read
with stdlib `tomllib`: hand-rolled escaping works until somebody names a challenge "Dijkstra's".
Every path is checked against `safe_filename` and its directory *before anything is opened* — a
`../`, an absolute path or a nested `tests/a/b.in` is a `ManifestError`, and `Storage.resolve` stays
the backstop rather than the check.

**`references/` is the one directory that mirrors rows, not `Storage`**: a reference's code is `submission.source`, so `references/sol.py` and `tests/sol.py` are different namespaces and the retired-file clash check doesn't apply. Each is a `[[reference]]` block (`file` required, `label` defaulting to the file name, `visibility` to `hidden`). **A reference is identified by its file name**, as a test case is by its input name, which is why `submission_reference_filename` makes it unique per challenge and the web Add refuses a duplicate or unsafe one. `plan_references` sorts by that: same name and same code (compared exactly, so git `autocrlf` replaces everything) is `updated` in place; same name and different code is `replaced` (code is immutable: delete the row and its outcomes, insert a fresh queued one); absent from the manifest is `deleted`. A stray file under `references/` or `tests/`, dotfiles included (`_pack` sends them), is refused as stray. Deletion is allowed here where a test case would be retired because a reference's outcomes can be reproduced by re-running it; "never strand a score" is about students. The push **locks, then refuses**: before writing anything it takes `FOR UPDATE` on every reference it would replace or delete (`lock_challenge_references`) and refuses if one is `running`, so a rejected push still changes nothing and the judge's `SKIP LOCKED` can't start a locked queued one mid-push. New and replaced references are queued and nothing waits; unchanged ones are not requeued, even if the test cases changed (the **stale** badge says so). Deletes run before inserts, or the unique index would refuse a replacement.

**A push can never strand a score**, which is the same rule `post_testcase_delete` enforces and the
reason `plan_testcases` sorts an absent test case into `retired` or `deleted` by `n_outcomes` alone:
graded means retired (row, files and outcomes all kept, `is_active` cleared), never graded means
deleted. A retired case *keeps its two files*, so a push that hands one of those names to a
different test case is refused before anything is written. Every other refusal happens up front too
— the manifest, the limits, the evaluator's language — so a rejected push leaves the challenge
exactly as it was. `execution_order` is then rewritten densely from the block order through negative
sentinels, for the reason `reorder_challenges` uses them.

The two ends share their rules rather than restating them. `_challenge_fields` validates the limits
and the ranking here as it does for the create form, and the editor's cards use its pieces
(`_limit_fields`, `_leaderboard_fields`, `_visible_from_value`) — one rule, not a second opinion — and `_check_evaluator_source` is the size/extension/UTF-8 chain, factored out of
`post_evaluator`, because a script the browser accepted and the CLI refused would be a difference
nobody could explain. `check_solution_bytes` is the same for solutions (size, extension, UTF-8), shared by the upload routes and the push's `references/` files.

**A pulled directory remembers where it came from, and a push is conditional on it.** `cocco pull`
writes `.cocco/state.toml` — the server's URL, the challenge id, the version it pulled and a
fingerprint per file — and a `.gitignore` of `*` beside it, because that version is what *this*
checkout agreed with the server on and a colleague's clone has agreed with nothing. The version is
the export's `ETag` and goes back as the push's `If-Match`, so a challenge edited on the web since
is a **412** where it used to be silently overwritten; an unchanged one answers a pull's
`If-None-Match` with a 304. It is **derived, never stored**: `_challenge_version` digests the
manifest, the statement, each stored file's *size and mtime* and each reference's file and **submission id** (code is immutable, so the id names the contents; a reference's grading is not in it, or a regrade would make every checkout stale), because a revision column would
have to be bumped by every route that touches a challenge or its files and the one that forgot
would be exactly the overwrite this exists to stop — and mtime rather than content, because gzip's
header timestamp makes identical plaintext differ on disk and reading every test case for a
precondition costs what the export does. A file rewritten with the same bytes therefore reads as a
change, which costs one pull that finds nothing to merge. A pull into a directory with state is a
**three-way comparison per file** (`reconcile` in `cli.py`): changed only on the server is taken,
changed only here is kept, changed on both is refused — or with `--force` taken after the local
copy is moved into `.cocco/discarded/` — and a file the server no longer has is removed, which is
what stops a stale `tests/` file breaking the next push. `challenge.toml` is fingerprinted as the
manifest it *means*, `dump_manifest(parse_manifest(…))`, since the server writes it its own way and
a byte comparison would make every hand-edited manifest a conflict. The URL in the state is
**compared and never used**: a token goes wherever the URL points and a directory is something
people hand around, so a mismatch is refused naming the `--url` to pass. `If-Match` stays optional
on the server — that is `cocco push --force`, and a zip pushed by hand — and the check is not in
the push's transaction, so a web edit landing inside that one request is still lost.

**Authentication is `ApiUser`, and it is the one identity in the app that is not a cookie.**
`get_api_user` tries `Authorization: Bearer`, then the session — that last so a signed-in browser
can follow a plain link to an export URL, and safe on the mutating half for the reason every other
POST here is, `same_site="lax"`. There is deliberately **no `DUMMY_AUTH` branch**, and there was
one: it came first and signed every API call in as the configured address, which meant a bearer
token presented to a development server was silently *ignored* — the one configuration in which the
header this function is mostly about did nothing. The cost is that `cocco pull` against a development server
now needs a token like anywhere else: mint one on the profile page and put it in `COCCO_TOKEN`. A
refusal is `ApiNotAuthenticated`, a plain `HTTPException(401)`, deliberately **not**
`NotAuthenticated`: that class is not an `HTTPException` precisely so its handler can choose between
a 303 and an `HX-Redirect`, and a CLI following the 303 would be handed a login page with a 200 on
it. `require_api_challenge_editor` is keyed on the challenge alone — with one id in the path there
is no second key to disagree with, `require_challenge_member`'s argument — and resolves through
`effective_challenge_role`, so an administrator is staff here as everywhere.

**A token is stored only as a hash** (`cocco/tokens.py`: `mint_token`, `hash_token`, `parse_bearer`),
so it is shown exactly once, when it is minted, because there is nowhere it could be read back from.
`api_token` is keyed on `user.id` rather than the address — the opposite of `Administrator` and for
the opposite reason: a token is minted on its owner's own profile page, so the account always
exists. `query_api_token_user` goes through `_USER_COLUMNS`, because that constant exists so that a
door which forgot `blocked_at` cannot be the one somebody walks through, and this is the third door.
The tokens section of `/users/{id}` is behind `ProfileOwner` and **not** `ProfileReader`: that page
already draws the line between oversight and impersonation, and listing somebody's credentials is on
the far side of it — an administrator sees no section at all. `last_used_at` is written on every
accepted call, which makes a GET write, and is the only thing that can tell an owner a token has
gone stale before they revoke it.

`cocco/cli.py` is `argparse` over `httpx`, run as `cocco` — `uv run cocco` in the checkout, plain
`cocco` where the package is installed, and it is the one of the three commands that needs nothing
but a URL and a token to be useful. `COCCO_URL` and `COCCO_TOKEN` are
its configuration, and that is a *client's* — nothing to do with `cocco.toml`, which is a
deployment's. `pull` extracts into a directory by default, because a directory is the thing you can
edit; `push` zips **only** `challenge.toml`, `README.md`, `tests/`, `evaluator/` and `references/`, so a `.git` or
an editor's swap file is never sent, and it checks every member's path on the way out of a zip even
though that zip came from our own server.
