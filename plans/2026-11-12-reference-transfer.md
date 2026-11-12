---
status: done
---

# Reference solutions in `cocco pull` / `push`

Follow-up to `2026-11-12-reference-solutions.md` ("Out of scope: Transfer").

## The problem

`cocco pull` and `push` ignore reference solutions. A challenge kept in git therefore loses its
references, and a challenge copied to next year's course (pull from one, push to the other) arrives
without them. A teacher who writes their reference in an editor beside `README.md` still has to
upload it through the browser.

What we want: a pulled challenge directory holds its references as files, `challenge.toml` says
how they are labelled and shown, and a push makes the server's references match the directory,
the same way it does for test cases.

## Decisions (from the interview)

- **Format.** A `references/` directory, flat like `tests/`, and one `[[reference]]` block per
  reference in `challenge.toml`:

  ```toml
  [[reference]]
  file = "references/dijkstra.cpp"
  label = "Teacher's Dijkstra"
  visibility = "results"        # hidden | results | source
  ```

  Only `file` is required. `label` defaults to the file name and `visibility` to `hidden`, the
  same defaults as the web **Add**, so adding a reference by hand takes two lines. A pull always
  writes all three keys. Language comes from the extension through `EXTENSION_TO_LANGUAGE`, as for
  any upload.
- **Identity is the file name**, as the input file name is for a test case. So a reference's
  `filename` must be **unique among a challenge's references** and **a bare safe name**
  (`safe_filename(name) == name`):
  - a partial unique index `(challenge_id, filename) WHERE reference_label IS NOT NULL`;
  - the web **Add** refuses a duplicate and an unsafe name with an alert (students' submissions
    are unaffected: they keep any name);
  - the **migration fails** if existing data breaks either rule, naming the offending
    `submission.id`s and saying how to fix them (rename the row or delete the reference). It does
    not rename anything itself. The feature is a few days old, so in practice there is nothing to fix.
- **Same name, same code** → an update: label and visibility are written, nothing is regraded.
- **Same name, different code** → **replace**: the old reference and its outcomes are deleted, and
  the new code is inserted as a fresh queued reference with the block's label and visibility. Code
  is immutable, and this is the web's "add a new one, delete the old" done in one step. Reported
  as `replaced`.
- **On the server but not in the manifest** → **deleted**, row and outcomes, as the web **Delete**
  does. Reported as `deleted`. "A push never strands a score" is about students' scores. A
  reference's outcomes can be reproduced by re-running it, which is why the web asks only for an
  `hx-confirm`.
- **A push that would delete or replace a reference the judge is running is refused**, up front
  with the other refusals, so a rejected push still changes nothing: "dijkstra.cpp is being graded
  right now. Push again when it has finished."
- **Grading: queue, don't wait.** New and replaced references are queued (`insert_reference`
  already notifies the judge). Unchanged ones are **not** requeued even when the same push changed
  the test cases: re-running stays manual, and the editor card's **stale** badge says when it's
  needed. `cocco push` prints what it queued and returns at once.
- **No results in the manifest.** `challenge.toml` stays configuration. A reference's score, time
  and staleness are on the editor card.

### Decisions taken without asking (say so if any is wrong)

- **The block is `[[reference]]`, singular**, like `[[testcase]]`, and the constants are
  `REFERENCES_DIR = "references"` in `transfer.py`.
- **Block order means nothing** on the server (the card lists by `id`). A pull writes them in `id`
  order, and a push inserts additions in block order, so a fresh push of a pulled directory
  reproduces the order.
- **Code comparison is exact**: the decoded file against `submission.source`, character for
  character. A checkout that rewrites line endings (git `autocrlf`) will therefore *replace* every
  reference on its next push. This costs a regrade but loses no data. `cli.md` says it in one
  sentence.
- **A pushed reference's `user_id` is the pusher** (`ApiUser`), as the web Add records whoever
  clicked.
- **The version covers references.** `challenge_version` digests the `[[reference]]` blocks (as
  part of the dumped manifest) and, per reference, its `file` and **submission id**. Code is
  immutable, so the id names the contents exactly, the way an exercise version's id does, and
  hashing the source is unnecessary. Adding, deleting, relabelling or changing the visibility of a
  reference on the web therefore makes the next conditional push a 412 instead of silently undoing
  it. A reference's *grading* (status, score) doesn't touch the version, so a regrade doesn't make
  every checkout stale.
- **Archived challenges accept a pushed reference**, as they accept one from the web and as a push
  already edits an archived challenge.
- **Locking.** Before writing any file, the push takes `FOR UPDATE` on every reference row it will
  delete or replace and refuses if one is `running`. The judge pulls with `SKIP LOCKED`, so a
  locked `queued` reference can't start mid-push. Lock order is submission before testoutcome, the
  judge's.
- **Reference files and test files are separate namespaces.** References are rows (`source` in
  the database) and never `Storage` files, so `references/sol.py` and `tests/sol.py` can't clash,
  and the retired-file clash check doesn't apply.
- **Test-case deletion still respects reference outcomes.** If a push removes a test case and
  replaces or deletes the references graded on it, `plan_testcases` sees `n_outcomes` from
  *before* the push, so the test case is **retired**, not deleted. That is the safe direction, and
  it's exactly what the web does if you delete the test case before the reference. Not worth an
  ordering subtlety.

## Steps

### 1. Schema: unique, safe reference file names

- `cocco/model.py`: an `Index("submission_reference_filename", Submission.c.challenge_id,
  Submission.c.filename, unique=True, postgresql_where=Submission.c.reference_label.is_not(None))`,
  with a comment saying it exists because the file name is a reference's identity in a pulled
  directory.
- `just migration "unique reference file names"`. Check by hand (`migrations.md`) that the
  `postgresql_where` survived autogenerate. **Before** `create_index`, an `op.execute` check:
  select references whose `filename` repeats within a challenge, or isn't a safe name (the same
  character class as `storage._UNSAFE`, a leading dot, over `MAX_FILENAME_LENGTH`), and raise with
  their ids and the sentence to act on. The safe-name test is spelled in SQL there and nowhere else.
  The migration says so, and the web check (below) is the Python one.
- `cocco/web/references.py` `post_reference`: after `read_solution_upload`, refuse with the card's
  alert if `safe_filename(filename) != filename` ("Name the file with letters, digits, dot, dash
  and underscore only. It becomes the file's name in `cocco pull`."), or if
  `query_reference_by_filename` (step 2) finds one ("A reference named dijkstra.cpp already exists:
  delete it first, or rename your file."). The unique index is the backstop for the race between
  that check and the insert. An `IntegrityError` there becomes the same alert, not a 500.

**Tests**: `test_reference_routes.py`: a duplicate name and an unsafe name each answer the card
with the alert and insert nothing. `test_repository.py`: two references with one file name on one
challenge is an `IntegrityError`, but the same name on two challenges and a student's submission
sharing it are both fine.

### 2. Repository

In `repository/challenges.py`:

- `query_challenge_reference_sources(conn, challenge_id)`: one row per reference in `id` order,
  with `id, filename, source, reference_label, reference_visibility, status`. It's the export's and
  the push's read, kept separate from `query_challenge_references` (the editor card's JSON
  document) because the card must never carry every reference's source on every poll.
- `query_reference_by_filename(conn, challenge_id, filename)` for the web check above.
- `lock_challenge_references(conn, submission_ids) -> list[int]`: `FOR UPDATE` on those rows,
  returning the ids that are `running`. The push calls it before writing anything.

`insert_reference`, `update_reference` and `lifecycle.delete_reference` are reused unchanged.
`delete_reference` returning False after the lock would be a bug, so the push asserts it.

### 3. `transfer.py`: the format and the plan

- `ManifestReference(file, label, visibility)` (frozen dataclass, `ReferenceVisibility`), with a
  `file_name` property like `ManifestTestCase.input_name`. `Manifest.references:
  tuple[ManifestReference, ...] = ()`.
- `parse_manifest`: `reference` must be a list of tables; `file` through `_member_path(…,
  REFERENCES_DIR, …)`; the extension must be in a known set, passed in as a `frozenset` argument
  rather than imported, because `transfer.py` touches nothing and `EXTENSION_TO_LANGUAGE` is built
  from `languages.toml`. The import route passes `EXTENSION_TO_LANGUAGE.keys()`. `label`:
  optional string, stripped, blank → the file name. `visibility`: optional, one of the enum
  values, unknown → `ManifestError` listing them. A file named twice → `ManifestError`.
- `dump_manifest`: after the test cases, one `[[reference]]` table each (all three keys), with
  the same blank-line spacing.
- `HEADER`: two more sentences: references live in `references/`; changing a file's code replaces
  the reference (and regrades it), and removing a block deletes it.
- `ExistingReference(id, file_name, source, label, visibility, status)` and
  `plan_references(existing, manifest, sources: Mapping[str, str]) -> ReferencePlan` with
  `updated` (same name, same code), `replaced` (same name, new code), `added`, `deleted`. Pure, like
  `plan_testcases`, with the identity rule in its docstring. `ReferencePlan.blocking(…)` returns the
  existing references in `replaced ∪ deleted` (the set the push must lock and check).
- `challenge_version` gains `references: Iterable[tuple[str, int]]` (file, submission id),
  digested after the files with the same length-prefixing.

**Tests** (`test_transfer.py`): round trip with references (dump → parse is identity); defaults
for a block with only `file`; refusals: path outside `references/`, nested path, unknown
extension, unknown visibility, duplicate file, `reference` not a list; `plan_references` for each
of its four outcomes, and a manifest with no `[[reference]]` deleting every existing one;
`challenge_version` changes with a reference's id and visibility and not with anything else about
it.

### 4. Export

`web/api.py`:

- `_ChallengeSnapshot` gains `references` (the rows from step 2), and the manifest it builds gains
  the `[[reference]]` blocks. `_challenge_version` passes `(f"references/{filename}", id)` pairs.
- `api_export_challenge`'s `build()` writes each reference's `source` as
  `references/{filename}` with `zf.writestr` (database text, so nothing to stream). `references/`
  is absent from the zip when there are none, as `evaluator/` is.

**Tests** (`test_api_routes.py`): the export of a challenge with a hidden and a `source` reference
carries both files and both blocks; the ETag changes after a web relabel, a visibility change, an
add and a delete, and **doesn't** after a reference is regraded.

### 5. Push

`web/api.py`:

- `_read_archive`: the referenced files must be present, and a stray file under `references/` is
  refused with the same sentence as a stray test file ("…add a [[reference]] block, or remove the
  file."). Each reference file is read here (≤ 1 MB, through the same size check as
  `read_solution_upload`, then UTF-8). That chain is factored out of `read_solution_upload` into
  `check_solution_bytes(filename, contents) -> tuple[language, source] | str` in
  `web/challenges.py`, so the browser, a student and the CLI can't disagree on what is accepted.
- `api_import_challenge`, in the refusal phase (before any file is written): build the
  `ReferencePlan`, `lock_challenge_references` on `plan.blocking()`, and refuse (400, nothing
  written, `db.rollback()`) if any of them is running.
- In the write phase, inside the same transaction as the test cases: `delete_reference` for
  `deleted` and `replaced`, `insert_reference` (`user_id=user.id`, which means the route gains
  `user: ApiUser` beside `role`) for `replaced` and `added` in block order, and `update_reference`
  for `updated` with the block's label and visibility. Deleting before inserting matters: the
  unique index would otherwise refuse a replacement.
- The response gains `"references": {"updated": n, "added": [...], "replaced": [...],
  "deleted": [...]}` with file names.

**Tests** (`test_api_routes.py`, the `make_client` pattern): a push that adds a reference queues
it as `hidden` with the pusher as `user_id`; an unchanged reference keeps its id, status and
outcomes; a relabel/visibility change keeps the id; changed code gives a new id, the old row and
outcomes gone; an absent block deletes; a `running` reference to be replaced or deleted makes the
push a 400 that leaves test cases, statement and references exactly as they were; a stray
`references/` file and a missing one are refused; an unknown extension is refused by
`parse_manifest`; a push on an archived challenge accepts references.

### 6. CLI

`cli.py`:

- `CHALLENGE.member_dirs` gains `REFERENCES_DIR`, which brings `references/` into `_pack`,
  `_fingerprints`, `reconcile` and `_extract` with no code of their own. Check that `_extract`
  creates the directory and that `reconcile` removes a reference file the server no longer has
  (the test-case case, so it should work already).
- `cmd_push`'s report prints the `references` summary. "queued" goes next to added and replaced,
  with a pointer to the editor card for results. Nothing waits.
- `cmd_pull`'s summary line counts references beside test cases.

**Tests** (`test_cli.py`): `_pack` includes `references/` and skips a stray dotfile in it; a pull
into a directory with state removes a reference file deleted on the server and keeps one edited
locally; the push report names replaced and deleted references.

### 7. Docs, guides, changelog

- `cocco/docs/cli.md` `{#zip-format}`: `references/` and the `[[reference]]` keys with their
  defaults; that the file name is the identity; that changing the code replaces and regrades, and
  removing a block deletes; the line-ending sentence.
- `cocco/docs/challenges.md` `{#references}`: one paragraph: references travel with `cocco pull` /
  `push`, and a reference's file name must be unique and plain (the web Add's two new refusals).
- `.claude/rules/transfer.md`: `references/` in the list of what the zip mirrors (it's the one
  directory that mirrors rows, not `Storage`), the identity rule, why deletion is allowed where a
  test case is retired, the lock-then-refuse step, and that the version names references by id.
- `.claude/rules/data-model.md`: the partial unique index and why. `domain-rules.md`'s references
  bullet: file names are unique and safe.
- Update the "Out of scope" line of `2026-11-12-reference-solutions.md` to point here.
- `CHANGELOG` entry.

## Out of scope

- Waiting for pushed references to be graded and printing their results (`cocco push --wait`),
  and a results column in `cocco ls`.
- Requeuing stale references on a push that changes test cases.
- Renaming a reference's file on the web (it's the identity; delete and re-add).

## Outcome (2026-11-12)

Implemented as planned, step by step, uncommitted on `feat/reference-solutions`. 1397 non-integration
tests and the migration tests pass. Migration `9c2f5908e7b7_unique_reference_file_names`.

Where the code differs from the plan:

- `parse_manifest(text, known_extensions=None)`: the extension set is optional, and `None` skips the
  check. The CLI only parses to fingerprint and has no language table; the API always passes
  `EXTENSION_TO_LANGUAGE`'s keys.
- `insert_reference` (through `insert_submission`) takes a `visibility`, so a pushed reference is
  inserted with its block's visibility in one write.
- `cocco push` does **not** skip a dotfile in `references/`: it sends it, as it does for `tests/`, and the
  server refuses it as stray. The stray-file sentence now reads "Add a [[testcase]] or [[reference]]
  block, or remove the file."
- The web Add's unsafe-name alert adds "(no leading dot)". The CLI prints the references summary
  only when the push touched one.
- `dump_manifest`'s blank line before a block is anchored to a header on its own line, because a
  label is free text and may contain `[[reference]]`.
- Fixtures in four existing tests gave several references on one challenge the same file name. They
  now use distinct names (the unique index).
