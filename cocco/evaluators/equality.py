#!/usr/bin/env python3
"""Exact match: 1 if the output is the expected one, 0 otherwise.

Bundled with cocco, and the evaluator a challenge is graded by when it has not
chosen one of its own.

    equality.py <actual_output> <expected_output> <test_input>

Prints one JSON object on stdout: {"score": <float>, "message": "<text>"}
"""

import json
import sys


def read(path: str) -> str:
    # A submitted program emits arbitrary bytes. A decode error must never be
    # the reason a checker crashes, so decode permissively and compare text.
    with open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def evaluate(actual: str, expected: str, test_input: str) -> tuple[float, str]:
    if actual.strip() == expected.strip():
        return 1.0, ""
    # Deliberately not quoting the expected output back: on a public sample it
    # would be redundant, and `diff.py` is the evaluator for "show me why".
    return 0.0, "The output is not the expected one."


if __name__ == "__main__":
    actual_path, expected_path, input_path = sys.argv[1:4]
    score, message = evaluate(
        read(actual_path), read(expected_path), read(input_path)
    )
    json.dump({"score": score, "message": message}, sys.stdout)
