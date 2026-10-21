"""Tests for the pure helpers of the HTTP layer. Nothing here needs a database
or a running app - the routes themselves are exercised by hand for now."""


import re

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match

from cocco.judge import load_languages
from cocco.main import app, render_markdown, render_source, static_url, visible_outcomes


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


def test_render_markdown_highlights_a_fenced_block():
    """A fence goes through Pygments and lands in the same `.cocco-code` scope
    a submission's source does, which is what makes the two look alike."""
    html = render_markdown("```python\ndef f():\n    return 1\n```\n")
    assert 'class="cocco-code"' in html
    assert '<span class="k">def</span>' in html


def test_render_markdown_leaves_an_unknown_language_plain():
    """A teacher writing ```pseudocode must get a code block, not a 500:
    codehilite falls back to the text lexer rather than raising."""
    html = render_markdown("```pseudocode\nfor each x in S\n```\n")
    assert 'class="cocco-code"' in html
    assert "for each x in S" in html
    assert '<span class="k">' not in html


def test_render_markdown_does_not_guess_the_language():
    """`guess_lang` is off, so a fence with no language stays plain - a block of
    sample *input* is the commonest thing a statement fences, and guessing would
    colour data as if it were code."""
    html = render_markdown("```\n3\n1 2 3\n```\n")
    assert 'class="cocco-code"' in html
    assert '<span class="k">' not in html
    assert '<span class="s">' not in html


def test_render_markdown_escapes_a_fenced_block():
    """The statement is staff-written and lands in the page as Markup, so
    Pygments' escaping is what stands between a fence and the DOM."""
    html = render_markdown("```python\n\"<script>alert(1)</script>\"\n```\n")
    assert "<script>" not in html
    assert "&lt;" in html and "&gt;" in html


def test_render_markdown_leaves_inline_code_alone():
    """Only blocks are highlighted; inline code stays a bare `<code>` for prose
    to style, with no wrapper of its own."""
    html = render_markdown("Call `solve(n)` with the input.\n")
    assert "<code>solve(n)</code>" in html
    assert "cocco-code" not in html


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
# event, which calls `prepare_database` against the real Postgres engine.
# The static mount needs none of it, and these stay unit tests.
_client = TestClient(app)


def test_static_url_carries_a_version():
    """The `?v=` is what opts a URL into a year of `immutable`, so a file that
    changes gets a URL that changes with it."""
    assert static_url("icon-small.png").startswith("/static/icon-small.png?v=")


def test_static_url_falls_back_when_the_file_is_missing():
    """`cocco/static/styles.css` is a build artifact and is simply absent until the
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


def _concrete_path(path: str) -> str:
    """The route's path with `1` in place of every parameter. `1` satisfies `str`,
    `int`, `float` and `path` converters, which are the only ones in use."""
    return re.sub(r"\{[^}]+\}", "1", path)


def test_no_route_is_shadowed_by_an_earlier_one():
    """Route order is load-bearing (`/courses/new` above `/courses/{course_id}`,
    every two-segment course page above `/courses/{course_id}/{challenge_id}`).
    For each route and method, Starlette's own matching must pick that route
    first. Skipped: entries with no `methods` (the `/static` Mount), which have
    no method to dispatch on."""
    routes = app.routes
    failures = []
    checked = 0
    for route in routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        path = _concrete_path(route.path)
        for method in sorted(methods):
            scope = {"type": "http", "path": path, "method": method, "root_path": ""}
            winner = next(
                (r for r in routes if r.matches(scope)[0] == Match.FULL), None
            )
            checked += 1
            if winner is not route:
                failures.append(
                    f"{method} {route.path} ({route.name}) is shadowed by "
                    f"{getattr(winner, 'path', None)} ({getattr(winner, 'name', None)}) "
                    f"for {path}"
                )
    assert checked > 100
    assert not failures, "\n".join(failures)
