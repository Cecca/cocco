"""Interactive exercises: the parts that are pure functions of their input.

An exercise set is authored as one Markdown file (`format.py` says how it is
spelled), graded by comparing a program's output with the expected one
(`compare.py`), explained to a student when it raises (`hints.py`), and run
inside an isolate box through `harness.py`. None of these modules touches the
database, FastAPI or the judge - the seam `transfer.py` already uses - so each
is testable by import. The compiled document they produce and read is
`schema.ExerciseDocument`.

`harness.py` is the odd one out: it is not imported by cocco at all. It is a
program the judge copies into a box and runs, so it has to stay plain stdlib
and self-contained, and it is resolved relative to this module for the reason
`cocco.evaluators.BUNDLED_DIR` is: an installed wheel has no working directory
to be relative to.
"""

from pathlib import Path

HARNESS_PATH = Path(__file__).resolve().parent / "harness.py"
