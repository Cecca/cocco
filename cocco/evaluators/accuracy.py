#!/usr/bin/env python3
"""Accuracy: reads both files as one label per line and scores the fraction of
lines that match.

    accuracy.py <actual_output> <expected_output> <test_input>

Prints one JSON object on stdout: {"score": <float>, "message": "<text>"}
"""

import json
import sys


def read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fp:
        return fp.read()


def evaluate(actual: str, expected: str, test_input: str) -> tuple[float, str]:
    ground_truth = expected.splitlines()
    actual_labels = actual.splitlines()
    total = len(ground_truth)
    if total == 0:
        return 1.0, ""
    # `zip` stops at the shorter one, so a short answer simply misses the
    # labels it never produced.
    matching = sum(
        1
        for truth, predicted in zip(ground_truth, actual_labels)
        if truth == predicted
    )
    return matching / total, f"Accuracy: {matching}/{total}"


if __name__ == "__main__":
    actual_path, expected_path, input_path = sys.argv[1:4]
    score, message = evaluate(
        read(actual_path), read(expected_path), read(input_path)
    )
    json.dump({"score": score, "message": message}, sys.stdout)
