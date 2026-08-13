"""The transfer format, tested by import.

`cocco/transfer.py` touches no database, no filesystem and no request, which is
exactly what lets the *format* - and the one rule that must not be got wrong,
that a graded test case is retired and never deleted - be checked here rather
than only through a round trip against a live server.
"""

import pytest

from cocco.model import LeaderboardRanking
from cocco.transfer import (
    HEADER,
    STATEMENT_NAME,
    TESTS_DIR,
    ExistingTestCase,
    Manifest,
    ManifestError,
    ManifestTestCase,
    dump_manifest,
    parse_manifest,
    plan_testcases,
)


def a_manifest(**overrides) -> Manifest:
    fields = dict(
        title="Two Sum",
        ranking=LeaderboardRanking.total_score,
        max_time_s=2.5,
        max_memory_mb=128.0,
        max_threads=1,
        evaluator=None,
        testcases=(
            ManifestTestCase("tests/01.in", "tests/01.out", is_sample=True),
            ManifestTestCase("tests/02.in", "tests/02.out", is_active=False),
        ),
    )
    fields.update(overrides)
    return Manifest(**fields)  # type: ignore[arg-type]


def an_existing(name: str, graded: int = 0, **overrides) -> ExistingTestCase:
    fields = dict(
        id=hash(name) % 10_000,
        input_name=f"{name}.in",
        expected_output_name=f"{name}.out",
        is_sample=False,
        is_active=True,
        n_outcomes=graded,
    )
    fields.update(overrides)
    return ExistingTestCase(**fields)  # type: ignore[arg-type]


# ── the manifest ────────────────────────────────────────────────────────────


def test_a_manifest_survives_a_round_trip():
    manifest = a_manifest(evaluator="evaluator/checker.py")
    assert parse_manifest(dump_manifest(manifest)) == manifest


def test_a_title_with_quotes_in_it_survives():
    """Which is why `dumps` writes this file and not an f-string: hand-rolled
    escaping works until somebody names a challenge "Dijkstra's"."""
    manifest = a_manifest(title='Dijkstra\'s "shortest" path')
    assert parse_manifest(dump_manifest(manifest)).title == manifest.title


def test_the_header_comments_are_ignored_on_the_way_back_in():
    text = dump_manifest(
        a_manifest(),
        HEADER.format(challenge_id=12, statement=STATEMENT_NAME, tests=TESTS_DIR),
    )
    assert text.startswith("#")
    assert parse_manifest(text) == a_manifest()


def test_the_block_order_is_the_execution_order():
    manifest = a_manifest()
    reversed_manifest = a_manifest(testcases=tuple(reversed(manifest.testcases)))
    parsed = parse_manifest(dump_manifest(reversed_manifest))
    assert [tc.input for tc in parsed.testcases] == ["tests/02.in", "tests/01.in"]


def test_an_unknown_ranking_is_refused_by_name():
    text = dump_manifest(a_manifest()).replace("total_score", "vibes")
    with pytest.raises(ManifestError, match="vibes"):
        parse_manifest(text)


@pytest.mark.parametrize("key", ["title", "ranking", "max_time_s", "max_threads"])
def test_a_missing_key_is_named(key):
    text = "\n".join(
        line for line in dump_manifest(a_manifest()).splitlines()
        if not line.startswith(f"{key} =")
    )
    with pytest.raises(ManifestError, match=key):
        parse_manifest(text)


def test_a_boolean_is_not_a_number():
    """`bool` is a subclass of `int`, so a `true` left where a limit belongs
    would otherwise pass as 1 and quietly become the time limit."""
    text = dump_manifest(a_manifest()).replace("max_threads = 1", "max_threads = true")
    with pytest.raises(ManifestError, match="max_threads"):
        parse_manifest(text)


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "tests/../../escape.in",
        "tests/nested/01.in",
        "01.in",
        "evaluator/01.in",
    ],
)
def test_a_path_outside_tests_is_refused_before_anything_is_opened(path):
    text = dump_manifest(a_manifest()).replace('"tests/01.in"', f'"{path}"')
    with pytest.raises(ManifestError):
        parse_manifest(text)


def test_a_name_safe_filename_would_rewrite_is_refused():
    """A manifest naming `a b.in` and a zip containing `a b.in` would otherwise
    be stored as `a_b.in`, which the next pull would not produce."""
    text = dump_manifest(a_manifest()).replace('"tests/01.in"', '"tests/a b.in"')
    with pytest.raises(ManifestError, match="usable file name"):
        parse_manifest(text)


def test_an_input_cannot_be_its_own_expected_output():
    text = dump_manifest(a_manifest()).replace('"tests/01.out"', '"tests/01.in"')
    with pytest.raises(ManifestError, match="different files"):
        parse_manifest(text)


def test_a_name_used_twice_is_refused():
    """Both files of every test case share one flat directory on disk, so a
    repeated name is one file standing in for two."""
    text = dump_manifest(a_manifest()).replace('"tests/02.in"', '"tests/01.in"')
    with pytest.raises(ManifestError, match="twice"):
        parse_manifest(text)


def test_no_evaluator_key_means_the_bundled_default():
    assert parse_manifest(dump_manifest(a_manifest())).evaluator is None
    assert "evaluator" not in dump_manifest(a_manifest())


# ── the plan ────────────────────────────────────────────────────────────────


def test_a_new_test_case_is_added_and_a_kept_one_is_updated():
    plan = plan_testcases([an_existing("01")], a_manifest())
    assert [u.existing.input_name for u in plan.updated] == ["01.in"]
    assert [a.entry.input for a in plan.added] == ["tests/02.in"]
    assert plan.retired == () and plan.deleted == ()


def test_the_order_comes_from_the_manifest_not_the_database():
    manifest = a_manifest()
    plan = plan_testcases([an_existing("02"), an_existing("01")], manifest)
    order = {u.existing.input_name: u.execution_order for u in plan.updated}
    assert order == {"01.in": 0, "02.in": 1}


def test_a_graded_test_case_dropped_from_the_manifest_is_retired():
    """The rule `post_testcase_delete` already enforces, and the reason a push
    can never strand a score: an outcome must keep pointing at the test case it
    was scored on."""
    graded = an_existing("99", graded=3)
    plan = plan_testcases([an_existing("01"), an_existing("02"), graded], a_manifest())
    assert [tc.input_name for tc in plan.retired] == ["99.in"]
    assert plan.deleted == ()


def test_an_ungraded_test_case_dropped_from_the_manifest_is_deleted():
    plan = plan_testcases(
        [an_existing("01"), an_existing("02"), an_existing("99")], a_manifest()
    )
    assert [tc.input_name for tc in plan.deleted] == ["99.in"]
    assert plan.retired == ()


def test_renaming_an_input_replaces_the_test_case():
    """Identity is the input file name - the manifest carries no ids, because
    it is meant to be edited and kept in git. The header comment says so."""
    plan = plan_testcases([an_existing("01"), an_existing("02")], a_manifest(
        testcases=(ManifestTestCase("tests/renamed.in", "tests/01.out"),)
    ))
    assert [a.entry.input for a in plan.added] == ["tests/renamed.in"]
    assert sorted(tc.input_name for tc in plan.deleted) == ["01.in", "02.in"]


def test_an_empty_manifest_retires_or_deletes_everything():
    plan = plan_testcases(
        [an_existing("01"), an_existing("02", graded=1)], a_manifest(testcases=())
    )
    assert [tc.input_name for tc in plan.deleted] == ["01.in"]
    assert [tc.input_name for tc in plan.retired] == ["02.in"]
