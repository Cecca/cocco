#!/usr/bin/env python3
"""Macro-averaged F1: reads both files as one label per line and scores the
unweighted mean of the per-class F1.

The label set is the union of the labels seen in the ground truth and in the
output. For each label, precision, recall and F1 are computed one-vs-rest; the
score is the mean of the per-class F1 values, so every class weighs the same
whatever its frequency. That is what makes it the right metric for an
imbalanced label distribution, where accuracy flatters a classifier that only
ever predicts the majority class.

    f1.py <actual_output> <expected_output> <test_input>

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
    if len(ground_truth) == 0:
        return 1.0, ""

    labels = set(ground_truth) | set(actual_labels)
    per_class = []
    lines = []
    for label in sorted(labels):
        tp = fp = fn = 0
        for truth, predicted in zip(ground_truth, actual_labels):
            if predicted == label and truth == label:
                tp += 1
            elif predicted == label and truth != label:
                fp += 1
            elif predicted != label and truth == label:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        per_class.append(f1)
        lines.append(f"{label}: P={precision:.3f} R={recall:.3f} F1={f1:.3f}")

    macro_f1 = sum(per_class) / len(per_class) if per_class else 0.0
    message = "\n".join(lines + [f"Macro-F1: {macro_f1:.3f}"])
    return macro_f1, message


if __name__ == "__main__":
    actual_path, expected_path, input_path = sys.argv[1:4]
    score, message = evaluate(
        read(actual_path), read(expected_path), read(input_path)
    )
    json.dump({"score": score, "message": message}, sys.stdout)
