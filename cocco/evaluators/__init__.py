"""The evaluator scripts that ship with cocco.

An evaluator is a program, not a Python class: the judge runs it inside isolate
exactly the way it runs a submission, so course staff can write one in any
language `languages.toml` knows about. The ones in this package are the
built-ins - they are ordinary scripts with no privileged status, and they double
as the worked examples a teacher copies and edits.

The contract, which every evaluator must honour:

    <script> <actual_output> <expected_output> <test_input>

three paths on argv, and one JSON object on stdout::

    {"score": 0.75, "message": "shown to the student"}

`message` is optional. Exiting non-zero is *never* "wrong answer" - it means the
checker itself broke, and the judge reports `evaluator_error` for that test.

A challenge whose `evaluator_path` is NULL is graded by `DEFAULT_EVALUATOR`
below, straight out of this directory. Nothing is copied into storage for that
case, so a fix here reaches every challenge that never chose its own checker.
"""

from pathlib import Path

# These files are code shipped with the judge, not user data: they live in the
# source tree and are resolved relative to this module, never to the working
# directory. `Storage` has no business with them and would refuse them anyway.
BUNDLED_DIR = Path(__file__).parent

# What a challenge with no evaluator of its own is graded by.
DEFAULT_EVALUATOR = "equality.py"

# name -> (title, one-line description), for the editor's picker. The keys are
# the only thing a request is ever matched against; see `bundled_path`.
BUNDLED: dict[str, tuple[str, str]] = {
    "equality.py": (
        "Exact match",
        "1 if the output equals the expected output, ignoring leading and "
        "trailing whitespace, and 0 otherwise. The default.",
    ),
    "diff.py": (
        "Line diff",
        "The fraction of expected lines that appear unchanged in the output, "
        "with the diff itself as the message. Gives partial credit.",
    ),
    "accuracy.py": (
        "Accuracy",
        "Reads both files as one label per line and scores the fraction of "
        "lines that match. For classification challenges.",
    ),
    "f1.py": (
        "Macro-F1",
        "Reads both files as one label per line and scores the unweighted mean "
        "of the per-class F1. For classification with imbalanced classes.",
    ),
}


def bundled_path(name: str) -> Path:
    """The path to a bundled evaluator, by name.

    A name that is not a key of `BUNDLED` raises, so a value that came from a
    request can never be joined onto `BUNDLED_DIR` and turned into a path.
    """
    if name not in BUNDLED:
        raise KeyError(name)
    return BUNDLED_DIR / name
