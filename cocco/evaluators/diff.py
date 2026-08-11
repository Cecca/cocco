#!/usr/bin/env python3
"""Line diff: the fraction of expected lines that survive the diff unchanged.

Gives partial credit, and puts the diff itself in the message.

    diff.py <actual_output> <expected_output> <test_input>

Prints one JSON object on stdout: {"score": <float>, "message": "<text>"}
"""

import json
import sys
from difflib import SequenceMatcher, unified_diff
from itertools import islice

# The diff is stored on the submission and shown to the student, so it is cut
# here rather than left for the judge to truncate: a diff of two large files is
# larger than either of them.
MAX_DIFF_LINES = 200


def read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def evaluate(actual: str, expected: str, test_input: str) -> tuple[float, str]:
    expected_lines = expected.splitlines(keepends=True)
    actual_lines = actual.splitlines(keepends=True)

    # `SequenceMatcher` rather than `Differ`: the two agree on which lines are
    # unchanged, but `Differ` recurses per replaced line and dies with a
    # RecursionError somewhere north of a couple of thousand of them - which is
    # exactly the size of output a partial-credit challenge produces.
    matcher = SequenceMatcher(None, expected_lines, actual_lines)
    matching = sum(block.size for block in matcher.get_matching_blocks())
    total = len(expected_lines)
    score = matching / total if total > 0 else 1.0

    # Lazily, so a diff of two large files is never built in full just to throw
    # all but the first screenful away.
    diff = unified_diff(
        expected_lines, actual_lines, fromfile="expected", tofile="actual"
    )
    shown = list(islice(diff, MAX_DIFF_LINES))
    if next(diff, None) is not None:
        shown.append("\n... diff truncated\n")
    return score, "".join(shown)


if __name__ == "__main__":
    actual_path, expected_path, input_path = sys.argv[1:4]
    score, message = evaluate(
        read(actual_path), read(expected_path), read(input_path)
    )
    json.dump({"score": score, "message": message}, sys.stdout)
