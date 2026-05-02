from typing import Annotated, Literal, Union, List
from pydantic import BaseModel, Field, TypeAdapter

class Evaluator(BaseModel):
    def evaluate(self, expected_output: str, actual_output: str) -> tuple[float, str]:
        raise NotImplementedError("Not implemented")



class EqualityEvaluator(Evaluator):
    type: Literal["equality"] = "equality"
    strip_whitespace: bool = True

    def evaluate(self, expected_output: str, actual_output: str) -> tuple[float, str]:
        expected_output = (
            expected_output.strip() if self.strip_whitespace else expected_output
        )
        actual_output = (
            actual_output.strip() if self.strip_whitespace else actual_output
        )
        if actual_output == expected_output:
            return (1, "")
        else:
            return (0, "Output is not the expected one:\n{expected_output}")


class DiffEvaluator(Evaluator):
    type: Literal["diff"] = "diff"
    def evaluate(self, expected_output: str, actual_output: str) -> tuple[float, str]:
        """Return the ratio of the diff lines as the score, and
        the actual diff as the message"""
        from difflib import Differ

        expected_lines = expected_output.splitlines(keepends=True)
        actual_lines = actual_output.splitlines(keepends=True)
        diff = list(Differ().compare(expected_lines, actual_lines))
        matching = sum(1 for line in diff if line.startswith("  "))
        total = len(expected_lines)
        score = matching / total if total > 0 else 1.0
        return score, "".join(diff)


class AccuracyEvaluator(Evaluator):
    """Interprets the text as a sequence of labels and compares the accuracy of
    a classificator."""

    type: Literal["accuracy"] = "accuracy"
    def evaluate(self, expected_output: str, actual_output: str) -> tuple[float, str]:
        ground_truth = expected_output.splitlines()
        actual_labels = actual_output.splitlines()
        total = len(ground_truth)
        if total == 0:
            return 1.0, ""
        matching = sum(
            1
            for expected, actual in zip(ground_truth, actual_labels)
            if expected == actual
        )
        score = matching / total
        return score, f"Accuracy: {matching}/{total}"


class F1Evaluator(Evaluator):
    """Interprets the text as a sequence of labels, one per line, and computes
    the macro-averaged F1 score of a classifier against the ground truth.

    The label set is the union of labels seen in the ground truth and in the
    actual output. For each label, precision, recall, and F1 are computed in a
    one-vs-rest fashion; the returned score is the unweighted mean of the
    per-class F1 values (macro-F1), so every class contributes equally
    regardless of its frequency. This makes the metric appropriate for
    imbalanced label distributions, where accuracy would be misleading.

    Score is in [0, 1]; the message contains per-class precision/recall/F1
    and the final macro-F1.
    """

    type: Literal["f1"] = "f1"

    def evaluate(self, expected_output: str, actual_output: str) -> tuple[float, str]:
        ground_truth = expected_output.splitlines()
        actual_labels = actual_output.splitlines()
        if len(ground_truth) == 0:
            return 1.0, ""

        labels = set(ground_truth) | set(actual_labels)
        per_class = []
        lines = []
        for label in sorted(labels):
            tp = fp = fn = 0
            for expected, actual in zip(ground_truth, actual_labels):
                if actual == label and expected == label:
                    tp += 1
                elif actual == label and expected != label:
                    fp += 1
                elif actual != label and expected == label:
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


class RMSEEvaluator(Evaluator):
    """Interprets both inputs as sequences of floats, one per line, and returns
    the Root Mean Squared Error between the actual output and the ground truth
    as the score.

    Unlike the other evaluators, a lower score indicates a better result (RMSE
    is in [0, +inf), with 0 meaning a perfect match). The message reports the
    RMSE, the number of paired samples, and any lines that could not be parsed
    as floats.
    """

    type: Literal["rmse"] = "rmse"

    def evaluate(self, expected_output: str, actual_output: str) -> tuple[float, str]:
        ground_truth = [float(line) for line in expected_output.splitlines()]
        actual_values: List[float] = []
        parse_errors: List[str] = []
        for i, line in enumerate(actual_output.splitlines()):
            try:
                actual_values.append(float(line))
            except ValueError:
                parse_errors.append(f"line {i + 1}: {line!r}")

        n = min(len(ground_truth), len(actual_values))
        if n == 0:
            return float("inf"), "No comparable float pairs"

        squared_errors = [
            (expected - actual) ** 2
            for expected, actual in zip(ground_truth[:n], actual_values[:n])
        ]
        rmse = (sum(squared_errors) / n) ** 0.5

        message_lines = [f"RMSE: {rmse:.6f}", f"Samples: {n}"]
        if len(ground_truth) != len(actual_values):
            message_lines.append(
                f"Length mismatch: expected {len(ground_truth)}, "
                f"got {len(actual_values)}"
            )
        if parse_errors:
            message_lines.append("Unparsable lines:")
            message_lines.extend(parse_errors)
        return rmse, "\n".join(message_lines)


AnyEvaluator = Annotated[
    Union[
        EqualityEvaluator, DiffEvaluator, AccuracyEvaluator, F1Evaluator, RMSEEvaluator
    ],
    Field(discriminator="type"),
]
evaluator_adapter: TypeAdapter[Evaluator] = TypeAdapter(AnyEvaluator)

