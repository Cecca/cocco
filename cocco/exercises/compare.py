"""How a program's output is compared with the expected one.

A port of `parseRules` / `compare` from the prototype's `comune.js`, under the
English rule names. The rules are declarative and the code that applies them is
ours, so the comparison runs in the judge process rather than in a box: an
evaluator program per test, as challenges have, would double the cost of a
check to buy nothing.

The rules, combined with commas on a `compare:` line:

    (none)        trailing spaces on each line and trailing blank lines are ignored
    exact         character for character
    spaces        runs of spaces count as one, and leading spaces are ignored
    case          upper and lower case are equivalent
    numbers 0.01  numbers may differ by at most the tolerance (default 1e-6);
                  a decimal comma is accepted
    order         lines may appear in any order
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

DEFAULT_TOLERANCE = 1e-6
RULE_NAMES = ("exact", "spaces", "case", "numbers", "order")


@dataclass(frozen=True)
class Rules:
    """A parsed `compare:` line. `numbers` is the tolerance, None when off.

    A dataclass rather than a Pydantic model so that this module needs nothing
    but the standard library; `schema.Exercise` embeds it as it is, and Pydantic
    validates and serializes a dataclass field without a second definition.
    """

    exact: bool = False
    spaces: bool = False
    case: bool = False
    numbers: float | None = None
    order: bool = False


def parse_rules(text: str | None) -> tuple[Rules, list[str]]:
    """The rules a `compare:` line names, and the parts that named none.

    An unknown part does not stop the others from applying; the caller decides
    whether it is an error (`format.py` says it is).
    """
    exact = spaces = case = order = False
    numbers: float | None = None
    unknown = []
    for part in (p.strip() for p in (text or "").split(",")):
        if not part:
            continue
        word, *args = part.split()
        match word.lower():
            case "exact":
                exact = True
            case "spaces":
                spaces = True
            case "case":
                case = True
            case "order":
                order = True
            case "numbers":
                tolerance = _tolerance(args[0]) if args else DEFAULT_TOLERANCE
                if tolerance is None:
                    unknown.append(part)
                else:
                    numbers = tolerance
            case _:
                unknown.append(part)
    rules = Rules(exact=exact, spaces=spaces, case=case, numbers=numbers, order=order)
    return rules, unknown


def _tolerance(text: str) -> float | None:
    # `_` is a Python spelling (`5_0`) that the prototype's JavaScript refused.
    if "_" in text:
        return None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def describe_rules(rules: Rules) -> list[str]:
    """One phrase per rule in force, for the sentence under an exercise."""
    out = [
        "exact comparison, character for character"
        if rules.exact
        else "trailing spaces and trailing blank lines do not matter"
    ]
    if rules.spaces:
        out.append("runs of spaces count as one")
    if rules.case:
        out.append("upper and lower case are equivalent")
    if rules.numbers is not None:
        out.append(f"numbers may differ by at most {rules.numbers:g}")
        if not rules.spaces:
            # `same_line` compares word by word once a tolerance is on, so
            # spacing within a line stops mattering - under `exact` too.
            out.append("spacing between words does not matter")
    if rules.order:
        out.append("the order of the lines does not matter")
    return out


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_lines(text: str, rules: Rules) -> list[str]:
    """The lines of `text` as the rules see them."""
    lines = _normalize_newlines(text).split("\n")
    if not rules.exact:
        lines = [line.rstrip(" \t") for line in lines]
        while lines and lines[-1] == "":
            lines.pop()
    if rules.spaces:
        lines = [re.sub(r"[ \t]+", " ", line.strip()) for line in lines]
    if rules.case:
        lines = [line.lower() for line in lines]
    return lines


# ASCII digits on purpose: `\d` would admit every script's digits, which the
# prototype's JavaScript did not.
_NUMBER = re.compile(r"[+-]?([0-9]+([.,][0-9]*)?|[.,][0-9]+)([eE][+-]?[0-9]+)?")


def same_line(a: str, b: str, rules: Rules) -> bool:
    """Whether two lines, already through `split_lines`, are equal."""
    if a == b:
        return True
    if rules.numbers is None:
        return False
    words_a, words_b = a.split(), b.split()
    if len(words_a) != len(words_b):
        return False
    return all(
        _same_word(x, y, rules.numbers) for x, y in zip(words_a, words_b)
    )


def _same_word(x: str, y: str, tolerance: float) -> bool:
    if x == y:
        return True
    if not _NUMBER.fullmatch(x) or not _NUMBER.fullmatch(y):
        return False
    nx = float(x.replace(",", "."))
    ny = float(y.replace(",", "."))
    # The relative term forgives the last bits of a float, so that a tolerance
    # of 0 still accepts the same number printed two ways.
    return abs(nx - ny) <= tolerance + 1e-12 * max(abs(nx), abs(ny))


@dataclass(frozen=True)
class Comparison:
    """The verdict, and which lines differ.

    `diff` holds line indexes into the *expected* output when `unordered` (the
    lines that found no partner) and into both outputs otherwise.
    """

    ok: bool
    diff: frozenset[int]
    unordered: bool = False


def compare(expected: str, actual: str, rules: Rules) -> Comparison:
    if rules == Rules(exact=True):
        e = _normalize_newlines(expected)
        a = _normalize_newlines(actual)
        el, al = e.split("\n"), a.split("\n")
        diff = frozenset(
            i
            for i in range(max(len(el), len(al)))
            if i >= len(el) or i >= len(al) or el[i] != al[i]
        )
        return Comparison(ok=e == a, diff=diff)

    el = split_lines(expected, rules)
    al = split_lines(actual, rules)
    if rules.order and rules.numbers is None:
        # Equality is the only test, so a multiset does in one pass what the
        # loop below does in expected x actual - which is seconds, for a few
        # hundred expected lines against a program that printed to the cap.
        available = Counter(al)
        missing = set()
        for i, line in enumerate(el):
            if available[line]:
                available[line] -= 1
            else:
                missing.add(i)
        return Comparison(
            ok=len(el) == len(al) and not missing,
            diff=frozenset(missing),
            unordered=True,
        )
    if rules.order:
        # Every expected line must find a distinct equal line, anywhere. With
        # a tolerance, "equal" is not an equivalence and cannot be hashed, so
        # this stays quadratic: the judge calls `compare` off the event loop.
        used = [False] * len(al)
        missing = set()
        for i, line in enumerate(el):
            j = next(
                (k for k, other in enumerate(al) if not used[k] and same_line(line, other, rules)),
                None,
            )
            if j is None:
                missing.add(i)
            else:
                used[j] = True
        return Comparison(
            ok=len(el) == len(al) and not missing,
            diff=frozenset(missing),
            unordered=True,
        )

    diff = frozenset(
        i
        for i in range(max(len(el), len(al)))
        if i >= len(el) or i >= len(al) or not same_line(el[i], al[i], rules)
    )
    return Comparison(ok=not diff, diff=diff)
