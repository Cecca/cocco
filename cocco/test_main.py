"""Tests for the pure helpers of the HTTP layer. Nothing here needs a database
or a running app - the routes themselves are exercised by hand for now."""


import pytest
from fastapi.testclient import TestClient

from cocco.judge import load_languages
from cocco.main import app, render_source, static_url, visible_outcomes


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


# Deliberately not `with TestClient(...)`: entering the context runs the startup
# event, which calls `create_db_and_tables` against the real Postgres engine.
# The static mount needs none of it, and these stay unit tests.
_client = TestClient(app)


def test_static_url_carries_a_version():
    """The `?v=` is what opts a URL into a year of `immutable`, so a file that
    changes gets a URL that changes with it."""
    assert static_url("icon-small.png").startswith("/static/icon-small.png?v=")


def test_static_url_falls_back_when_the_file_is_missing():
    """`static/styles.css` is a build artifact and is simply absent until the
    first `npm run build:css`; an unversioned URL still has to work."""
    assert static_url("no-such-file.png") == "/static/no-such-file.png"


def test_versioned_static_is_cached_forever():
    response = _client.get("/static/icon-small.png?v=abc")
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("url", ["/static/icon-small.png", "/static/icon-small.png?nv=1"])
def test_unversioned_static_is_revalidated(url):
    """Without a version there is nothing to invalidate a stale copy, so it must
    be revalidated - and `?nv=1` is the near-miss that a substring test for
    "v=" would wrongly cache for a year."""
    assert _client.get(url).headers["cache-control"] == "no-cache"
