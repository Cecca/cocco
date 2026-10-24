"""The exercise-writing skill teachers download, tested by import.

The skill is prose about `format.py`, and prose drifts: these tests hold it to
the parser. Its examples must import cleanly, and every keyword, comparison
rule and language the parser knows must be named in it. The route that zips
it is in `test_exercise_routes.py`.
"""

import pytest

from cocco.exercises.compare import RULE_NAMES
from cocco.exercises.format import FRONT_MATTER_KEYS, RESERVED, parse
from cocco.languages import load_languages
from cocco.web.exercise_sets import EXERCISE_SKILL_DIR, EXERCISE_SKILL_NAME

SKILL = (EXERCISE_SKILL_DIR / "SKILL.md").read_text()
EXAMPLES = sorted((EXERCISE_SKILL_DIR / "examples").glob("*.md"))


def test_the_skill_is_named_after_its_directory():
    front_matter = SKILL.split("---")[1]
    assert f"name: {EXERCISE_SKILL_NAME}\n" in front_matter
    assert "description: " in front_matter


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda path: path.name)
def test_every_example_imports_without_a_problem(example):
    parsed = parse(example.read_text())
    assert parsed.problems == []
    assert parsed.document is not None
    # Named after its language, so the skill can point at the right one.
    assert parsed.document.language == example.stem


def test_the_examples_are_the_ones_the_skill_names():
    for example in EXAMPLES:
        assert f"examples/{example.name}" in SKILL


@pytest.mark.parametrize(
    "word", [*RESERVED, "hidden", "compare:", "time:", *(f"{k}:" for k in FRONT_MATTER_KEYS)]
)
def test_every_keyword_is_documented(word):
    assert f"`{word}" in SKILL


@pytest.mark.parametrize("rule", RULE_NAMES)
def test_every_comparison_rule_is_documented(rule):
    assert f"| `{rule}" in SKILL


@pytest.mark.parametrize("language", load_languages())
def test_every_language_is_documented(language):
    assert f"**`{language}`**" in SKILL
