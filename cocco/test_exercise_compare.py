"""Output comparison and error hints, tested by import.

The cases are lifted from the behaviour of the prototype's `comune.js`, whose
`parseRules` / `compare` / `errorHint` these modules port.
"""

import pytest

from cocco.exercises.compare import (
    DEFAULT_TOLERANCE,
    Rules,
    compare,
    describe_rules,
    parse_rules,
    split_lines,
)
from cocco.exercises.hints import error_hint


def rules(text: str) -> Rules:
    parsed, unknown = parse_rules(text)
    assert unknown == []
    return parsed


# ---- parse_rules --------------------------------------------------------------


def test_no_rules_is_the_default():
    assert parse_rules("") == (Rules(), [])
    assert parse_rules(None) == (Rules(), [])


def test_rules_combine_with_commas_case_insensitively():
    assert rules("spaces, CASE ,order") == Rules(spaces=True, case=True, order=True)
    assert rules("exact") == Rules(exact=True)


def test_numbers_takes_a_tolerance_or_a_default():
    assert rules("numbers").numbers == DEFAULT_TOLERANCE
    assert rules("numbers 0.01").numbers == 0.01
    assert rules("numbers 0").numbers == 0


def test_a_decimal_comma_in_the_tolerance_splits_the_line():
    # As in the prototype: rules are split on commas before anything else, so
    # the decimal comma is accepted in the *output* and not on this line.
    parsed, unknown = parse_rules("numbers 0,5")
    assert parsed.numbers == 0
    assert unknown == ["5"]


@pytest.mark.parametrize(
    "bad", ["numbers -1", "numbers abc", "numbers inf", "numbers nan", "numbers 0_1"]
)
def test_a_bad_tolerance_is_unknown(bad):
    parsed, unknown = parse_rules(bad)
    assert parsed.numbers is None
    assert unknown == [bad]


def test_unknown_rules_are_reported_and_the_rest_still_apply():
    parsed, unknown = parse_rules("spaces, esatto, sorted")
    assert parsed == Rules(spaces=True)
    assert unknown == ["esatto", "sorted"]


def test_describe_rules():
    assert describe_rules(Rules()) == [
        "trailing spaces and trailing blank lines do not matter"
    ]
    assert describe_rules(rules("exact, numbers 0.01, order"))[1:] == [
        "numbers may differ by at most 0.01",
        "spacing between words does not matter",
        "the order of the lines does not matter",
    ]


def test_exact_with_numbers_ignores_spacing_within_a_line_as_described():
    r = rules("exact, numbers")
    assert compare("1 2\n", "1   2\n", r).ok
    assert "spacing between words does not matter" in describe_rules(r)


# ---- compare ------------------------------------------------------------------


def test_default_ignores_trailing_spaces_and_trailing_blank_lines():
    assert compare("a\nb\n", "a  \nb\t\n\n\n", Rules()).ok
    assert compare("a\r\nb", "a\nb\n", Rules()).ok


def test_default_does_not_ignore_leading_spaces_or_blank_lines_in_between():
    result = compare("a\nb\n", " a\n\nb\n", Rules())
    assert not result.ok
    assert result.diff == {0, 1, 2}


def test_default_marks_missing_and_extra_lines():
    assert compare("a\nb\nc\n", "a\nX\n", Rules()).diff == {1, 2}
    assert compare("a\n", "a\nb\n", Rules()).diff == {1}


def test_exact_is_character_for_character_except_newline_style():
    assert compare("a\nb\n", "a\r\nb\r\n", rules("exact")).ok
    result = compare("a\nb\n", "a \nb", rules("exact"))
    assert not result.ok
    assert result.diff == {0, 2}


def test_exact_with_another_rule_keeps_trailing_blank_lines_significant():
    assert not compare("a\n", "a \n", rules("exact, case")).ok
    assert not compare("1\n", "1\n\n", rules("exact, numbers")).ok
    assert compare("1.0\n", "1\n", rules("exact, numbers")).ok


def test_spaces_collapses_runs_and_ignores_leading_spaces():
    assert compare("a b  c\n", "  a   b\tc\n", rules("spaces")).ok
    assert not compare("ab\n", "a b\n", rules("spaces")).ok


def test_case():
    assert compare("Hello World\n", "hello WORLD\n", rules("case")).ok
    assert not compare("Hello\n", "hello\n", Rules()).ok


def test_numbers_within_tolerance_word_by_word():
    r = rules("numbers 0.01")
    assert compare("area 3.14\n", "area 3.141592\n", r).ok
    assert compare("3.14\n", "3,145\n", r).ok
    assert not compare("3.14\n", "3.2\n", r).ok
    # Words that are not numbers still have to match exactly.
    assert not compare("area 3.14\n", "Area 3.14\n", r).ok
    assert not compare("1 2\n", "1 2 3\n", r).ok


def test_numbers_default_tolerance_and_number_spellings():
    r = rules("numbers")
    assert compare("0.5\n", ".5\n", r).ok
    assert compare("1\n", "1.\n", r).ok
    assert compare("100\n", "1e2\n", r).ok
    assert compare("-0.1\n", "-0.1000001\n", r).ok
    assert not compare("0.1\n", "0.10001\n", r).ok
    assert not compare("1\n", "one\n", r).ok
    assert not compare("1\n", "0x1\n", r).ok


def test_numbers_zero_tolerance_forgives_only_float_noise():
    r = rules("numbers 0")
    assert compare("0.3\n", "0.30000000000000004\n", r).ok
    assert not compare("0.3\n", "0.31\n", r).ok


def test_order_accepts_any_permutation():
    result = compare("a\nb\nc\n", "c\na\nb\n", rules("order"))
    assert result.ok
    assert result.unordered


def test_order_matches_each_line_once():
    result = compare("a\na\nb\n", "a\nb\nb\n", rules("order"))
    assert not result.ok
    assert result.diff == {1}


@pytest.mark.parametrize(
    "expected, actual",
    [
        ("a\nb\nc\n", "c\na\nb\n"),
        ("a\na\nb\n", "a\nb\nb\n"),
        ("a\nb\n", "b\na\nc\n"),
        ("x\ny\n", ""),
        ("", "x\n"),
    ],
)
def test_order_without_a_tolerance_agrees_with_the_general_path(expected, actual):
    # A zero tolerance takes the quadratic path while still meaning "equal"
    # for lines with no numbers on them.
    fast = compare(expected, actual, rules("order"))
    general = compare(expected, actual, rules("order, numbers 0"))
    assert (fast.ok, fast.diff) == (general.ok, general.diff)


def test_order_is_not_quadratic_without_a_tolerance():
    expected = "".join(f"{i}\n" for i in range(500))
    actual = "".join(f"{i}\n" for i in range(120_000))
    result = compare(expected, actual, rules("order"))
    assert not result.ok
    assert result.diff == set()


def test_order_with_an_extra_line_fails_with_nothing_to_mark():
    result = compare("a\nb\n", "b\na\nc\n", rules("order"))
    assert not result.ok
    assert result.diff == set()


def test_rules_combine():
    r = rules("spaces, case, numbers 0.1, order")
    assert compare("Total  1.0\nCount 2\n", "count 2\ntotal 1.05\n", r).ok


def test_split_lines():
    assert split_lines("A  b \n\n", rules("spaces, case")) == ["a b"]
    assert split_lines("a \n", rules("exact")) == ["a ", ""]


# ---- error_hint ---------------------------------------------------------------


def test_error_hint_with_a_line():
    assert error_hint("ZeroDivisionError", 4) == "Line 4: division by zero."


def test_error_hint_without_a_line_is_capitalized():
    assert error_hint("NameError", None).startswith("Name not defined")


def test_error_hint_names_an_exception_it_cannot_explain():
    assert error_hint("FloatingPointError", 2) == "Line 2: FloatingPointError error."
    assert error_hint("StopIteration") == "StopIteration error."


def test_error_hint_of_nothing_is_empty():
    assert error_hint(None) == ""
    assert error_hint("") == ""
