"""The bundled evaluator scripts, and the contract every evaluator honours.

None of this needs isolate: the scripts are ordinary programs, so they are
exercised as ordinary programs. That the sandbox can also run them is what the
integration tests in `test_judge.py` are for.
"""


import subprocess
import sys
from pathlib import Path

import pytest

import cocco.judge as cj
from cocco.evaluators import BUNDLED, DEFAULT_EVALUATOR, bundled_path
from cocco.schema import EvaluatorResult


LANGUAGES = cj.load_languages(Path("languages.toml"))


def run_bundled(
    name: str, tmp_path: Path, actual: str, expected: str, test_input: str = ""
) -> EvaluatorResult:
    """Run a bundled evaluator the way the judge does, and parse what it said."""
    paths = []
    for label, contents in (
        ("actual", actual),
        ("expected", expected),
        ("input", test_input),
    ):
        path = tmp_path / label
        path.write_text(contents)
        paths.append(str(path))

    proc = subprocess.run(
        [sys.executable, str(bundled_path(name)), *paths],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return cj.parse_evaluator_output(proc.stdout)


# ── the registry ────────────────────────────────────────────────────────


def test_every_bundled_evaluator_exists_and_has_a_known_language():
    for name in BUNDLED:
        assert bundled_path(name).is_file()
        assert cj.language_for_filename(name, LANGUAGES) is not None


def test_the_default_is_one_of_the_bundled_ones():
    assert DEFAULT_EVALUATOR in BUNDLED


def test_bundled_path_refuses_anything_not_in_the_registry():
    # The editor takes this name straight from a form field, so a name that is
    # not a key must never become a path.
    for name in ("../../etc/passwd", "equality", "unknown.py", ""):
        with pytest.raises(KeyError):
            bundled_path(name)


@pytest.mark.parametrize("name", sorted(BUNDLED))
def test_bundled_evaluator_honours_the_contract(name: str, tmp_path: Path):
    """Whatever it is handed, it exits 0 and prints one valid result."""
    result = run_bundled(name, tmp_path, "a\nb\nc", "a\nb\nc")
    assert isinstance(result.score, float)


@pytest.mark.parametrize("name", sorted(BUNDLED))
@pytest.mark.parametrize(
    "actual,expected",
    [
        ("", ""),
        ("", "a\nb"),
        ("a\nb", ""),
        ("a\nb\nc\nd", "a\nb"),
        ("x", "y"),
    ],
    ids=["both-empty", "no-output", "no-expectation", "too-long", "wrong"],
)
def test_bundled_evaluator_survives_edge_cases(
    name: str, actual: str, expected: str, tmp_path: Path
):
    result = run_bundled(name, tmp_path, actual, expected)
    assert 0.0 <= result.score <= 1.0


@pytest.mark.parametrize("name", sorted(BUNDLED))
def test_bundled_evaluator_survives_non_utf8_output(name: str, tmp_path: Path):
    """A submitted program can emit any bytes at all; that must not be what
    makes the checker crash."""
    actual = tmp_path / "actual"
    actual.write_bytes(b"\xff\xfe not text \x00")
    expected = tmp_path / "expected"
    expected.write_text("hello")
    test_input = tmp_path / "input"
    test_input.write_text("")

    proc = subprocess.run(
        [
            sys.executable,
            str(bundled_path(name)),
            str(actual),
            str(expected),
            str(test_input),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert cj.parse_evaluator_output(proc.stdout).score == 0.0


# ── the scores themselves, pinned against the classes these replaced ─────


def test_equality_scores(tmp_path: Path):
    assert run_bundled("equality.py", tmp_path, "hello moon\n", "hello moon").score == 1.0
    assert run_bundled("equality.py", tmp_path, "hello sun", "hello moon").score == 0.0


def test_equality_does_not_leak_the_expected_output(tmp_path: Path):
    """The class this replaced returned a message with an uninterpolated
    `{expected_output}` in it. Neither the placeholder nor the real thing."""
    result = run_bundled("equality.py", tmp_path, "wrong", "the secret answer")
    assert "{expected_output}" not in result.message
    assert "the secret answer" not in result.message


def test_diff_scores_matching_lines(tmp_path: Path):
    result = run_bundled(
        "diff.py", tmp_path, "one\nfour\nthree\nfour", "one\ntwo\nthree\nfour"
    )
    assert result.score == 0.75
    assert "two" in result.message


def test_diff_caps_a_runaway_message(tmp_path: Path):
    """Two files that share nothing, at the size where `difflib.Differ` used to
    die with a RecursionError."""
    result = run_bundled(
        "diff.py",
        tmp_path,
        "\n".join(str(n) for n in range(5000)),
        "\n".join(str(-n) for n in range(5000)),
    )
    assert result.score < 0.01
    assert "diff truncated" in result.message
    assert len(result.message.splitlines()) < 300


def test_accuracy_scores(tmp_path: Path):
    result = run_bundled("accuracy.py", tmp_path, "a\nb\nc", "a\nx\nc")
    assert result.score == pytest.approx(2 / 3)
    assert result.message == "Accuracy: 2/3"


def test_f1_is_macro_averaged(tmp_path: Path):
    # Two classes, each with precision 1.0/recall 0.5 or the reverse: both F1s
    # are 2/3, so the macro average is too.
    result = run_bundled("f1.py", tmp_path, "a\nb\nb", "a\nb\na")
    assert result.score == pytest.approx(2 / 3)
    assert "Macro-F1" in result.message


def test_f1_weighs_a_rare_class_as_much_as_a_common_one(tmp_path: Path):
    """The whole reason to prefer it to accuracy: always predicting the
    majority class scores well on accuracy and badly here."""
    expected = "a\n" * 9 + "b\n"
    always_a = "a\n" * 10
    assert run_bundled("accuracy.py", tmp_path, always_a, expected).score == 0.9
    assert run_bundled("f1.py", tmp_path, always_a, expected).score < 0.5


# ── the result contract ─────────────────────────────────────────────────


def test_parse_evaluator_output_accepts_a_bare_score():
    result = cj.parse_evaluator_output('{"score": 0.25}')
    assert result.score == 0.25
    assert result.message == ""


def test_parse_evaluator_output_ignores_extra_keys():
    # So the contract can grow without invalidating the scripts already out
    # there.
    assert cj.parse_evaluator_output('{"score": 1, "detail": {"x": 2}}').score == 1.0


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "looks good to me",
        "0.5",
        "[0.5]",
        '{"message": "no score"}',
        '{"score": "quite good"}',
        '{"score": 1} trailing garbage',
        # Python spells these; JSON does not, and no other language emits them.
        '{"score": Infinity}',
        '{"score": NaN}',
    ],
    ids=[
        "empty",
        "prose",
        "bare-number",
        "array",
        "no-score",
        "unparsable-score",
        "trailing-garbage",
        "infinity",
        "nan",
    ],
)
def test_parse_evaluator_output_rejects(stdout: str):
    with pytest.raises(ValueError):
        cj.parse_evaluator_output(stdout)


def test_a_score_outside_zero_one_is_allowed():
    # Not every metric is a fraction; the aggregates that assume one are what
    # is wrong in that case, not the score.
    assert cj.parse_evaluator_output('{"score": 42.5}').score == 42.5
    assert cj.parse_evaluator_output('{"score": -1}').score == -1.0


# ── language detection, shared with submitted solutions ─────────────────


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("checker.py", "python"),
        ("Checker.java", "java"),
        ("CHECK.PY", "python"),
        ("checker.cc", "cpp"),
        ("checker.txt", None),
        ("checker", None),
        ("", None),
    ],
)
def test_language_for_filename(filename: str, expected: str | None):
    language = cj.language_for_filename(filename, LANGUAGES)
    assert (language.name if language else None) == expected


# ── failure descriptions ────────────────────────────────────────────────


def _outcome(status: str, oom: bool = False, stderr: str = "") -> cj.RunOutcome:
    return cj.RunOutcome(
        retcode=1,
        stdout="",
        stderr=stderr,
        elapsed_time_s=0.0,
        max_rss_kb=0,
        status=status,
        message=None,
        oom=oom,
    )


def test_evaluator_failures_are_described_as_the_evaluators():
    """Never as a verdict on the submission: `_classify_isolate_failure` would
    turn a checker's timeout into `OutcomeEnum.timeout`, which would put a
    bogus row on the running_time leaderboard."""
    assert "timed out" in cj._describe_evaluator_failure(_outcome("TO"))
    assert "out of memory" in cj._describe_evaluator_failure(
        _outcome("SG", oom=True)
    )
    assert "exited with status" in cj._describe_evaluator_failure(_outcome("RE"))


def test_evaluator_failure_quotes_stderr():
    assert "boom" in cj._describe_evaluator_failure(_outcome("RE", stderr="boom"))
