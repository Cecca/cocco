"""Interactive exercises: the parts that are pure functions of their input.

An exercise set is authored as one Markdown file (`format.py` says how it is
spelled), graded by comparing a program's output with the expected one
(`compare.py`), and explained to a student when it fails (`hints.py`). The
program itself runs in an isolate box through `Language.compile` and
`Language.run`, as a challenge's does. None of these modules touches the
database, FastAPI or the judge - the seam `transfer.py` already uses - so each
is testable by import. The compiled document they produce and read is
`schema.ExerciseDocument`.
"""
