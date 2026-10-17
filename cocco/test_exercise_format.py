"""The exercise file format, tested by import.

The fixtures are written for these tests. The prototype's own `sorgenti/*.md`
hold real solutions to exercises a class is given, and are not copied in.
"""

import json

import pytest

from cocco.exercises.compare import Rules
from cocco.exercises.format import DEFAULT_TIME_S, parse
from cocco.schema import EXERCISE_FORMAT, ExerciseDocument

A_SET = """\
---
title: Loops
description: Lab of 14 October
---

Read the *whole* statement.

# Sum of a line

Print the sum of the numbers on one line.

```python
print(sum([1, 2]))
```

```starter
line = input()
```

```python solution
print(sum(int(x) for x in input().split()))
```

```input
3 4 5
```

```input hidden
-1 -2 -3
```

```output
-6
```

```hint
Use `split()`.
```

```hint
Then `int()`.
```

compare: numbers 0.01, order
time: 2.5 s

# Fail loudly

Raise on a negative number.

```solution
n = int(input())
if n < 0:
    raise ValueError(n)
print(n)
```

```input
4
```

```input
-1
```

```error
ValueError
```

```input hidden
-2
```

```output
```

```error
```
"""


def parsed(source: str, **kwargs) -> ExerciseDocument:
    result = parse(source, **kwargs)
    assert result.document is not None, result.problems
    return result.document


def problems(source: str, **kwargs) -> list[tuple[str, str]]:
    return [(p.level, p.text) for p in parse(source, **kwargs).problems]


def errors(source: str, **kwargs) -> list[str]:
    result = parse(source, **kwargs)
    found = [p.text for p in result.problems if p.level == "error"]
    assert (result.document is None) == bool(found)
    return found


def test_a_whole_set():
    result = parse(A_SET)
    assert result.problems == []
    doc = result.document
    assert doc is not None
    assert doc.format == EXERCISE_FORMAT
    assert doc.language == "python"
    assert doc.title == "Loops"
    assert doc.description == "Lab of 14 October"
    assert doc.intro == "Read the *whole* statement."
    assert [e.title for e in doc.exercises] == ["Sum of a line", "Fail loudly"]

    first, second = doc.exercises
    # Any other code block stays part of the statement, fences and all.
    assert first.statement == (
        "Print the sum of the numbers on one line.\n\n"
        "```python\nprint(sum([1, 2]))\n```"
    )
    assert first.starter == "line = input()\n"
    assert first.solution == "print(sum(int(x) for x in input().split()))\n"
    assert first.hints == ["Use `split()`.", "Then `int()`."]
    assert first.rules == Rules(numbers=0.01, order=True)
    assert first.time_s == 2.5
    assert first.line == 8
    assert [(t.input, t.hidden, t.output, t.error, t.line) for t in first.tests] == [
        ("3 4 5\n", False, None, None, 24),
        ("-1 -2 -3\n", True, "-6\n", None, 28),
    ]

    assert second.starter == ""
    assert second.rules == Rules()
    assert second.time_s == DEFAULT_TIME_S
    assert [(t.input, t.hidden, t.output, t.error) for t in second.tests] == [
        ("4\n", False, None, None),
        ("-1\n", False, None, "ValueError"),
        # An empty `output` is an empty expectation; an empty `error` is
        # "whatever the solution raises".
        ("-2\n", True, "", ""),
    ]


def test_the_parser_leaves_the_expected_outputs_to_the_build():
    for exercise in parsed(A_SET).exercises:
        for test in exercise.tests:
            assert test.expected_output is None
            assert test.expected_error is None


def test_the_document_round_trips_through_json():
    doc = parsed(A_SET)
    stored = json.loads(doc.model_dump_json())
    assert stored["format"] == 1
    assert stored["exercises"][0]["rules"] == {
        "exact": False, "spaces": False, "case": False, "numbers": 0.01, "order": True,
    }
    assert ExerciseDocument.model_validate(stored) == doc


def test_a_document_of_another_format_is_refused():
    stored = json.loads(parsed(A_SET).model_dump_json())
    stored["format"] = 2
    with pytest.raises(ValueError):
        ExerciseDocument.model_validate(stored)


def test_line_endings_and_a_bom_do_not_matter():
    crlf = "﻿" + A_SET.replace("\n", "\r\n")
    assert parsed(crlf) == parsed(A_SET)


def test_an_exercise_with_no_input_gets_one_empty_implicit_test():
    doc = parsed("---\ntitle: T\n---\n# Hello\n\nSay hi.\n\n```solution\nprint('hi')\n```\n")
    (test,) = doc.exercises[0].tests
    assert test.input == ""
    assert test.implicit
    assert test.line == 4


def test_a_statement_heading_of_a_lower_level_does_not_start_an_exercise():
    doc = parsed("---\ntitle: T\n---\n# One\n\n## Notes\n\ntext\n\n```solution\npass\n```\n")
    assert len(doc.exercises) == 1
    assert doc.exercises[0].statement.startswith("## Notes")


def test_a_heading_inside_a_code_block_does_not_start_an_exercise():
    source = "---\ntitle: T\n---\n# One\n\n~~~~\n# a comment\n```\n~~~~\n\n```solution\npass\n```\n"
    doc = parsed(source)
    assert len(doc.exercises) == 1
    assert doc.exercises[0].statement == "~~~~\n# a comment\n```\n~~~~"


def test_output_and_error_must_follow_an_input():
    source = "---\ntitle: T\n---\n# One\n\n```input\n1\n```\n\nSome text.\n\n```output\n1\n```\n```solution\npass\n```\n"
    assert errors(source) == ['"output" block at line 12 with no "input" block right before it.']


def test_output_may_follow_error():
    source = "---\ntitle: T\n---\n# One\n\n```input\n1\n```\n```error\nKeyError\n```\n```output\nx\n```\n"
    (test,) = parsed(source).exercises[0].tests
    assert (test.output, test.error) == ("x\n", "KeyError")


def test_two_outputs_for_one_input_is_an_error():
    source = "---\ntitle: T\n---\n# One\n```input\n1\n```\n```output\n1\n```\n```output\n2\n```\n"
    assert errors(source) == ['Two "output" blocks for the same input (line 11).']


def test_an_error_block_takes_a_class_name():
    source = "---\ntitle: T\n---\n# One\n```solution\npass\n```\n```input\n1\n```\n```error\nValueError: bad\n```\n"
    (error,) = errors(source)
    assert '"ValueError: bad" is not an exception name' in error


def test_a_missing_solution_is_an_error_unless_every_output_is_written():
    no_solution = "---\ntitle: T\n---\n# One\n\nText.\n\n```input\n1\n```\n"
    (error,) = errors(no_solution)
    assert 'The "solution" block is missing' in error

    written = no_solution + "```output\n1\n```\n"
    assert parsed(written).exercises[0].solution is None

    # An empty `error` block asks the solution which exception it is.
    asks = written + "```error\n```\n"
    assert errors(asks)


def test_reserved_blocks_before_the_first_exercise_are_an_error():
    source = "---\ntitle: T\n---\n```solution\npass\n```\n# One\n```solution\npass\n```\n"
    assert errors(source) == [
        'The "solution" block at line 4 comes before the first exercise (# Title).'
    ]


def test_an_unclosed_block_is_an_error():
    source = "---\ntitle: T\n---\n# One\n```solution\npass\n"
    assert "Code block opened at line 5 and never closed." in errors(source)


def test_no_exercises_is_an_error():
    assert errors("---\ntitle: T\n---\nJust an intro.\n") == [
        'No exercises found: each exercise starts with a "# Title" line.'
    ]


@pytest.mark.parametrize("line", ["compare: sorted", "compare: numbers x"])
def test_an_unknown_comparison_rule_is_an_error(line):
    source = f"---\ntitle: T\n---\n# One\n```solution\npass\n```\n{line}\n"
    (error,) = errors(source)
    assert error.startswith("Unknown comparison rule:")
    assert "exact, spaces, case, numbers, order" in error


@pytest.mark.parametrize(
    "value, seconds",
    [("3", 3.0), ("0.5", 0.5), ("1,5", 1.5), ("10 s", 10.0), ("10 seconds", 10.0), ("120", 120.0)],
)
def test_time_is_seconds(value, seconds):
    source = f"---\ntitle: T\n---\n# One\n```solution\npass\n```\ntime: {value}\n"
    assert parsed(source).exercises[0].time_s == seconds


@pytest.mark.parametrize("value", ["0", "-1", "121", "five", "inf", "5_0"])
def test_a_bad_time_is_an_error(value):
    source = f"---\ntitle: T\n---\n# One\n```solution\npass\n```\ntime: {value}\n"
    (error,) = errors(source)
    assert error.startswith(f'"time: {value}" is not valid')


def test_option_lines_before_the_first_exercise_are_intro_text():
    doc = parsed("---\ntitle: T\n---\ntime: whenever\n# One\n```solution\npass\n```\n")
    assert doc.intro == "time: whenever"


def test_warnings_come_with_a_document():
    source = (
        "---\ntitle: T\nauthor: me\n---\n"
        "# One\n"
        "```starter\na\n```\n```starter\nb\n```\n"
        "```solution\npass\n```\n"
        "```hidden\n1\n```\n"
        "```input hidden\n1\n```\n"
    )
    result = parse(source)
    assert result.document is not None
    assert result.document.exercises[0].starter == "b\n"
    assert [(p.level, p.where) for p in result.problems] == [
        ("warning", "front matter"),
        ("warning", 'exercise 1, "One"'),
        ("warning", 'exercise 1, "One"'),
        ("warning", 'exercise 1, "One"'),
    ]
    texts = [p.text for p in result.problems]
    assert texts[0] == 'Unknown key "author", ignored. The keys are title, description.'
    assert texts[1] == 'More than one "starter" block: using the last one.'
    assert texts[2] == 'A "hidden" block with no "input": write ```input hidden```.'
    assert texts[3] == "Every test is hidden: students will not see a single example."
    # The stray block is any other code block: part of the statement.
    assert result.document.exercises[0].statement == "```hidden\n1\n```"


def test_an_empty_statement_is_a_warning():
    assert problems("---\ntitle: T\n---\n# One\n```solution\npass\n```\n") == [
        ("warning", "The statement is empty.")
    ]


def test_an_unrecognized_front_matter_line_is_a_warning():
    assert ("warning", 'Unrecognized line: "just words".') in problems(
        "---\ntitle: T\njust words\n---\n# One\nx\n```solution\npass\n```\n"
    )


def test_a_missing_title_falls_back_to_the_default_with_a_warning():
    source = "# One\nx\n```solution\npass\n```\n"
    result = parse(source, default_title="lab-03")
    assert result.document is not None
    assert result.document.title == "lab-03"
    assert [p.level for p in result.problems] == ["warning"]
    assert errors(source) == [
        'The set has no title: add a "title: …" line between the two "---" at the top.'
    ]


def test_a_front_matter_with_no_end_is_intro_text():
    result = parse("---\ntitle: T\n# One\nx\n```solution\npass\n```\n", default_title="d")
    assert result.document is not None
    assert result.document.title == "d"
    assert result.document.intro == "---\ntitle: T"


# ---- the prototype's Italian keywords -------------------------------------------
#
# Not aliases: an error naming the English word. Silently accepted, the first
# two below would put a solution and a hidden test in front of every student.


@pytest.mark.parametrize(
    "block, english",
    [
        ("soluzione", "solution"),
        ("python soluzione", "solution"),
        ("iniziale", "starter"),
        ("suggerimento", "hint"),
        ("errore", "error"),
        ("input nascosto", "hidden"),
    ],
)
def test_an_italian_block_is_an_error(block, english):
    source = f"---\ntitle: T\n---\n# One\n```solution\npass\n```\n```input\n1\n```\n```{block}\nx\n```\n"
    (error,) = errors(source)
    assert f'write "{english}"' in error


@pytest.mark.parametrize("line, english", [("confronto: spazi", "compare"), ("tempo: 5", "time")])
def test_an_italian_option_is_an_error(line, english):
    source = f"---\ntitle: T\n---\n# One\n```solution\npass\n```\n{line}\n"
    (error,) = errors(source)
    assert f'write "{english}:"' in error


def test_an_italian_front_matter_key_is_an_error():
    source = "---\ntitolo: Cicli\n---\n# One\n```solution\npass\n```\n"
    assert '"titolo:" is not a key: write "title:".' in errors(source, default_title="x")


def test_a_nul_character_is_an_error_naming_its_lines():
    source = "---\ntitle: T\n---\n# One\nA\x00B\n```solution\nprint('\x00')\n```\n"
    assert errors(source) == ["A NUL character (\\0) at lines 5, 7: remove it."]


def test_the_title_problem_comes_first():
    result = parse("# One\nText.\n```solution\npass\n```\ncompare: sorted\n")
    assert [p.where for p in result.problems] == ["front matter", 'exercise 1, "One"']


def test_problems_say_which_exercise_and_test_they_are_about():
    source = (
        "---\ntitle: T\n---\n"
        "# One\nText.\n```solution\npass\n```\n"
        "# Two\nText.\n```solution\npass\n```\n"
        "```input\n1\n```\n```input\n2\n```\n```output\na\n```\n```output\nb\n```\n"
        "compare: sorted\n"
    )
    result = parse(source)
    assert [(p.exercise, p.test) for p in result.problems] == [(2, 2), (2, None)]
    assert parse("---\ntitle: T\n---\n").problems[0].exercise is None
