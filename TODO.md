# TODO

Failure-handling gaps in `cocco/judge.py::run_submission` (and its caller
`run_from_queue`), to be tackled one by one.

## Compile-failure `TestOutcome` is sparse

`run_submission` constructs the compile-failure outcomes without
`elapsed_s`, `peak_memory_bytes`, `stdout`, or `stderr` fields. Verify
that the model defaults are sensible (e.g. `0` / `None`) and decide
whether the compile stdout/stderr should populate the dedicated fields
instead of being concatenated into `message`.
