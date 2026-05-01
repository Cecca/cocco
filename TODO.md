# TODO

Failure-handling gaps in `cocco/judge.py::run_submission` (and its caller
`run_from_queue`), to be tackled one by one.

## Run-path failures collapse into a single `failure` outcome

`judge.py:517-518` (the existing `# TODO: handle timed out runs` /
`# TODO: handle out of memory runs`).

Anything with `exec_res.status != "OK"` becomes `OutcomeEnum.failure`, and
`eval_msg` is hardcoded to `""`. Isolate's status codes carry meaningful
distinctions:

- `TO` — time limit exceeded
- `SG` — killed by signal (typically OOM under `--cg`)
- `RE` — non-zero exit (runtime error)
- `XX` — isolate internal error

At minimum, propagate `exec_res.status` and `exec_res.message` into the
`TestOutcome.message` field. Consider distinct `OutcomeEnum` values for
timeout / OOM / runtime-error so the UI can render them differently.

## Evaluator exceptions are not caught

`judge.py:531` calls `evaluator.evaluate(...)` with no `try/except`. Any
evaluator bug or malformed ground-truth file propagates up and aborts the
whole submission.

Concrete example: `RMSEEvaluator.evaluate` (`judge.py:448`) does
`float(line)` on the *expected* output with no guard, so a malformed
ground-truth file raises `ValueError`.

Wrap evaluator calls and turn evaluator failures into a judge-internal-error
outcome (or similar) instead of letting them escape.

## Storage I/O is unguarded

`storage.resolve(test_case.input_path)` (`judge.py:511`) and
`storage.read(test_case.expected_output_path)` (`judge.py:532`) will raise
if a referenced test file is missing or unreadable. Same blast radius as
the evaluator case — kills the whole submission.

## Unknown language → `KeyError`

`load_languages(Path("languages.toml"))[submission.language]`
(`judge.py:495`) crashes if the submission's `language` field doesn't match
any entry in the TOML, or if `languages.toml` itself is missing/malformed.
Should be reported as a per-submission failure, not a runner crash.

## Binary stdout/stderr → `UnicodeDecodeError`

`isolate_run` opens the captured stdout/stderr files in text mode
(`judge.py:173-176`). A submission that writes non-UTF-8 bytes will raise
on read. Either open in binary and decode with `errors="replace"`, or
catch and substitute a placeholder.

## Compile-failure `TestOutcome` is sparse

`judge.py:561-569` constructs the compile-failure outcomes without
`elapsed_s`, `peak_memory_bytes`, `stdout`, or `stderr` fields. Verify
that the model defaults are sensible (e.g. `0` / `None`) and decide
whether the compile stdout/stderr should populate the dedicated fields
instead of being concatenated into `message`.

## Caller leaves submissions stuck in `running` on exception

`run_from_queue` (`judge.py:575`) has no `try/except` around
`run_submission`. Any of the failures above (or anything else that
escapes) leaves the row in `SubmissionStatus.running` forever; the next
poll just picks up the next submission. Operator intervention is needed
to unstick rows.

Wrap the call so that on exception the submission is marked `done` (with
a judge-internal-error outcome attached) or a new `failed` status, and
the transaction is committed.

## `isolate_run` uses hard `assert`s on isolate sub-calls

`judge.py:122` (init) and `judge.py:209` (cleanup) assert on the isolate
returncode. If the host runs out of box ids, isolate is misconfigured, or
cleanup races with something, the assertion crashes the coroutine — same
stuck-submission problem as above. Convert to explicit error handling so
the failure is attributable to the submission rather than crashing the
worker.
