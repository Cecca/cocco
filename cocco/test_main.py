"""Tests for the pure helpers of the HTTP layer. Nothing here needs a database
or a running app - the routes themselves are exercised by hand for now."""


import pytest

from cocco.judge import load_languages
from cocco.main import render_source, visible_outcomes


def _outcome(is_sample: bool, **overrides):
    outcome = {
        "execution_order": 0,
        "is_sample": is_sample,
        "is_active": True,
        "outcome": "runtime_error",
        "score": 0.0,
        "elapsed_s": 0.1,
        "peak_memory_bytes": 1 << 20,
        "message": "Output is not the expected one:\n42\n",
        "stdout": "41\n",
        "stderr": "boom",
    }
    outcome.update(overrides)
    return outcome


@pytest.mark.parametrize("language", sorted(load_languages()))
def test_render_source_knows_every_language(language):
    """Every name in languages.toml is a Pygments alias too, which is the whole
    reason the submission's `language` column can be used as the lexer name."""
    html = render_source("int x = 1;\n", language)
    # Highlighted, not just escaped: there is at least one token span
    assert '<span class="' in html


def test_render_source_falls_back_to_plain_text():
    html = render_source("some words\n", "cobol-from-the-future")
    assert "some words" in html


def test_render_source_escapes_the_source():
    """The source is a student's upload and lands in the page as Markup, so
    Pygments' escaping is the only thing between it and the DOM."""
    html = render_source("<script>alert(1)</script>\n", "python")
    # The angle brackets come back escaped - as separate tokens, so the tag is
    # never contiguous in the output either way
    assert "<script>" not in html
    assert "&lt;" in html and "&gt;" in html


def test_visible_outcomes_redacts_hidden_test_cases_for_students():
    """A hidden test case gives away nothing but its verdict: `message` can be
    the evaluator's diff of the expected output, and stdout/stderr are the
    student's own program, which is how a hidden input would be exfiltrated."""
    hidden, sample = _outcome(is_sample=False), _outcome(is_sample=True)

    redacted, untouched = visible_outcomes([hidden, sample], is_staff=False)

    assert (redacted["message"], redacted["stdout"], redacted["stderr"]) == (
        None,
        None,
        None,
    )
    # The verdict and the numbers survive - a student has to know what failed
    assert redacted["outcome"] == "runtime_error"
    assert redacted["score"] == 0.0
    assert redacted["elapsed_s"] == 0.1
    assert redacted["peak_memory_bytes"] == 1 << 20
    # A public sample is not redacted: its input and expected output are
    # downloadable from the challenge page anyway
    assert untouched == sample
    # ...and the input list is left alone rather than mutated in place
    assert hidden["message"] is not None


def test_visible_outcomes_shows_everything_to_staff():
    outcomes = [_outcome(is_sample=False), _outcome(is_sample=True)]
    assert visible_outcomes(outcomes, is_staff=True) == outcomes
