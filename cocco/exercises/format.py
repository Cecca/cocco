"""The exercise file format: a set of exercises spelled as one Markdown file.

A port of the prototype's `formato.js`, with English keywords and one spelling
for each - there are no Italian aliases. The file is the source of truth and is
stored verbatim; this module turns it into the compiled document's shape
(`schema.ExerciseDocument`) minus the expected outputs, which only running the
solution can produce, or into the problems that stop it from being one.

    ---
    title: Loops and lists
    description: Lab of 14 October
    ---

    Optional introduction, shown before the first exercise.

    # Sum of a line                 starts an exercise; file order is the order

    The statement, in Markdown.

    ```starter                      the editor's initial contents (optional)
    ```solution                     the author's; never shown to a student
    ```input                        a visible test: stdin
    ```input hidden                 a hidden test
    ```output                       right after an input: the expected output,
                                    written out instead of computed
    ```error                        right after an input (or its output): the
                                    exception the program must end with; empty
                                    means "whatever the solution raises"
    ```hint                         one per hint, any number

    compare: numbers 0.01           see `compare.py`
    time: 5                         seconds per test, default 5

`python starter` / `python solution` also work. Any other code block stays part
of the statement, and an exercise with no `input` gets one test with empty input.

Nothing here runs Python. The checks that need it - a solution that crashes, a
starter that already passes - belong to the judge's build.
"""

import re
from dataclasses import dataclass, field

from cocco.exercises.compare import RULE_NAMES, parse_rules
from cocco.schema import (
    Exercise,
    ExerciseDocument,
    ExerciseProblem,
    ExerciseTest,
)

DEFAULT_TIME_S = 5.0
MAX_TIME_S = 120.0

FRONT_MATTER_KEYS = ("title", "description")
RESERVED = ("starter", "solution", "input", "output", "error", "hint")

# The prototype's keywords. They are not aliases: a file still spelling them is
# a file whose conversion was missed, and a ```soluzione block left as it is
# would be "any other code block" - part of the statement, solution and all, on
# every student's screen. So they are an error that names the English word.
_ITALIAN = {
    "iniziale": "starter",
    "soluzione": "solution",
    "nascosto": "hidden",
    "errore": "error",
    "suggerimento": "hint",
    "confronto": "compare",
    "tempo": "time",
    "titolo": "title",
    "descrizione": "description",
}

_FENCE_OPEN = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*$")
_HEADING = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_OPTION = re.compile(r"^\s*(compare|time)\s*:\s*(.*)$", re.IGNORECASE)
_ITALIAN_OPTION = re.compile(r"^\s*(confronto|tempo)\s*:", re.IGNORECASE)
_FRONT_MATTER_LINE = re.compile(r"^\s*([A-Za-zÀ-ÿ_]+)\s*:\s*(.*)$")
_EXCEPTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECONDS = re.compile(r"\s*s(econds?)?$", re.IGNORECASE)


@dataclass(frozen=True)
class ParseResult:
    """The document, or None when any problem is an error, and every problem.

    Warnings can come with a document; errors never do, so a caller cannot
    store a version the author has been told is broken.
    """

    document: ExerciseDocument | None
    problems: list[ExerciseProblem]


@dataclass
class _Fence:
    marker: str
    info: str
    start: int
    raw: str
    body: list[str] = field(default_factory=list)


@dataclass
class _Draft:
    """An exercise while its lines are still being read."""

    title: str
    line: int
    text: list[str] = field(default_factory=list)
    starter: str | None = None
    solution: str | None = None
    tests: list[ExerciseTest] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    compare: str = ""
    time_s: float | None = None


def _block(body: str) -> str:
    """A block's contents as a file would hold them: a final newline unless empty."""
    return body + "\n" if body else ""


def parse(source: str, default_title: str = "") -> ParseResult:
    """Parse an exercise file.

    `default_title` stands in for a missing `title:` (the import passes the
    uploaded file's name, as the prototype used the set's id), with a warning;
    a set that ends up with no title at all is an error, because a version is
    listed by it.
    """
    problems: list[ExerciseProblem] = []

    def problem(level, where, text, exercise=None, test=None):
        problems.append(ExerciseProblem(
            level=level, where=where, text=text, exercise=exercise, test=test,
        ))

    text = source.replace("\r\n", "\n").replace("\r", "\n").removeprefix("\ufeff")
    lines = text.split("\n")

    # PostgreSQL cannot store a NUL in `text` or in JSONB, and the source and
    # the document are both stored. Nothing an author means to type is one.
    nul_lines = [str(k + 1) for k, line in enumerate(lines) if "\x00" in line]
    if nul_lines:
        problem(
            "error", "file",
            f"A NUL character (\\0) at line{'s' if len(nul_lines) > 1 else ''} "
            f"{', '.join(nul_lines[:10])}"
            f"{', …' if len(nul_lines) > 10 else ''}: remove it.",
        )

    i = 0
    meta: dict[str, str] = {}

    # Optional front matter between two `---` lines.
    if lines and lines[0].strip() == "---":
        end = next((k for k in range(1, len(lines)) if lines[k].strip() == "---"), None)
        if end is not None:
            for line in lines[1:end]:
                m = _FRONT_MATTER_LINE.match(line)
                if not m:
                    if line.strip():
                        problem("warning", "front matter", f'Unrecognized line: "{line.strip()}".')
                    continue
                key = m.group(1).lower()
                if key in _ITALIAN:
                    problem(
                        "error", "front matter",
                        f'"{key}:" is not a key: write "{_ITALIAN[key]}:".',
                    )
                elif key not in FRONT_MATTER_KEYS:
                    problem(
                        "warning", "front matter",
                        f'Unknown key "{key}", ignored. The keys are '
                        f'{", ".join(FRONT_MATTER_KEYS)}.',
                    )
                else:
                    meta[key] = m.group(2).strip()
            i = end + 1

    drafts: list[_Draft] = []
    intro: list[str] = []
    cur: _Draft | None = None
    fence: _Fence | None = None
    # The test an `output` or `error` block would attach to: the last `input`,
    # for as long as nothing but blank lines and those two blocks follow it.
    last_input: ExerciseTest | None = None

    def here(level, text, test=None):
        """A problem in whatever the parser is reading now."""
        if cur is None:
            problem(level, "introduction", text)
        else:
            n = len(drafts)
            problem(level, f'exercise {n}, "{cur.title}"', text, exercise=n, test=test)

    def close_fence(fence: _Fence, close: str) -> None:
        nonlocal last_input
        ex = cur
        info = fence.info.lower().split()
        body = "\n".join(fence.body)
        words = info[1:] if info[:1] in (["python"], ["py"]) else info
        kind = words[0] if words else None
        line_no = fence.start + 1
        # 1-based, for a block about the test it would attach to.
        test_no = len(ex.tests) if ex is not None and last_input is not None else None

        # Every word, not only the first: ```input nascosto is an `input`, and
        # one that would quietly be a *visible* test.
        for word in words:
            if word in _ITALIAN:
                here(
                    "error",
                    f'Block "{fence.info}" at line {line_no}: write "{_ITALIAN[word]}" '
                    f'instead of "{word}".',
                )
        if ex is None or kind not in RESERVED:
            if ex is not None and kind == "hidden":
                here("warning", 'A "hidden" block with no "input": write ```input hidden```.')
            (ex.text if ex else intro).extend([fence.raw, *fence.body, close])
            if ex is None and kind in RESERVED:
                here(
                    "error",
                    f'The "{kind}" block at line {line_no} comes before the first exercise (# Title).',
                )
        elif kind == "starter":
            if ex.starter is not None:
                here("warning", 'More than one "starter" block: using the last one.')
            ex.starter = _block(body)
        elif kind == "solution":
            if ex.solution is not None:
                here("warning", 'More than one "solution" block: using the last one.')
            ex.solution = _block(body)
        elif kind == "input":
            test = ExerciseTest(input=_block(body), hidden="hidden" in words, line=line_no)
            ex.tests.append(test)
            last_input = test
            return
        elif kind == "output":
            if last_input is None:
                here("error", f'"output" block at line {line_no} with no "input" block right before it.')
            elif last_input.output is not None:
                here("error", f'Two "output" blocks for the same input (line {line_no}).', test_no)
            else:
                last_input.output = _block(body)
            # `output` and `error` may follow each other after the same input.
            return
        elif kind == "error":
            name = body.strip()
            if last_input is None:
                here("error", f'"error" block at line {line_no} with no "input" block right before it.')
            elif last_input.error is not None:
                here("error", f'Two "error" blocks for the same input (line {line_no}).', test_no)
            elif name and not _EXCEPTION_NAME.fullmatch(name):
                here(
                    "error",
                    f'"error" block at line {line_no}: "{name}" is not an exception name. '
                    "Write only the class name, for example ValueError, or leave the "
                    "block empty to use the one the solution raises.",
                    test_no,
                )
            else:
                last_input.error = name
            return
        elif kind == "hint":
            ex.hints.append(body)
        last_input = None

    for i in range(i, len(lines)):
        line = lines[i]
        if fence is not None:
            m = _FENCE_CLOSE.match(line)
            if m and m.group(2)[0] == fence.marker[0] and len(m.group(2)) >= len(fence.marker):
                close_fence(fence, line)
                fence = None
            else:
                fence.body.append(line)
            continue
        m = _FENCE_OPEN.match(line)
        if m:
            fence = _Fence(marker=m.group(2), info=m.group(3).strip(), start=i, raw=line)
            continue
        m = _HEADING.match(line)
        if m:
            cur = _Draft(title=m.group(1).strip(), line=i + 1)
            drafts.append(cur)
            last_input = None
            continue
        m = _OPTION.match(line)
        if m and cur is not None:
            value = m.group(2).strip()
            if m.group(1).lower() == "compare":
                cur.compare = value
            else:
                seconds = _seconds(value)
                if seconds is None:
                    here(
                        "error",
                        f'"time: {value}" is not valid: write a number of seconds, '
                        f"more than 0 and at most {MAX_TIME_S:g}.",
                    )
                else:
                    cur.time_s = seconds
            continue
        m = _ITALIAN_OPTION.match(line)
        if m and cur is not None:
            key = m.group(1).lower()
            here(
                "error",
                f'Line {i + 1}: "{key}:" is not an option: write "{_ITALIAN[key]}:". '
                "As it stands it would be part of the statement.",
            )
        if line.strip():
            last_input = None
        (cur.text if cur else intro).append(line)

    if fence is not None:
        here("error", f"Code block opened at line {fence.start + 1} and never closed.")

    # Per-exercise checks that do not need Python.
    exercises = []
    for n, draft in enumerate(drafts, start=1):
        label = f'exercise {n}, "{draft.title}"'
        statement = "\n".join(draft.text).strip()
        rules, unknown = parse_rules(draft.compare)
        if unknown:
            problem(
                "error", label,
                f"Unknown comparison rule: {', '.join(unknown)}. "
                f"The rules are {', '.join(RULE_NAMES)}.",
                exercise=n,
            )
        if not statement:
            problem("warning", label, "The statement is empty.", exercise=n)
        tests = draft.tests or [ExerciseTest(input="", line=draft.line, implicit=True)]
        if draft.solution is None and any(t.output is None or t.error == "" for t in tests):
            problem(
                "error", label,
                'The "solution" block is missing: it is needed to compute the expected '
                'output of the tests (and the expected error, where an "error" block is empty).',
                exercise=n,
            )
        if all(t.hidden for t in tests):
            problem(
                "warning", label,
                "Every test is hidden: students will not see a single example.",
                exercise=n,
            )
        exercises.append(Exercise(
            title=draft.title,
            statement=statement,
            starter=draft.starter or "",
            solution=draft.solution,
            hints=draft.hints,
            rules=rules,
            time_s=draft.time_s or DEFAULT_TIME_S,
            tests=tests,
            line=draft.line,
        ))
    if not exercises:
        problem("error", "file", 'No exercises found: each exercise starts with a "# Title" line.')

    # First in the list, whatever else is wrong: it is about the whole set.
    title = meta.get("title", "")
    if not title:
        title = default_title.strip()
        if title:
            problems.insert(0, ExerciseProblem(
                level="warning", where="front matter",
                text=f'The set has no title, so it is called "{title}": add a '
                '"title: …" line between the two "---" at the top.',
            ))
        else:
            problems.insert(0, ExerciseProblem(
                level="error", where="front matter",
                text='The set has no title: add a "title: …" line between the two '
                '"---" at the top.',
            ))

    if any(p.level == "error" for p in problems):
        return ParseResult(document=None, problems=problems)
    document = ExerciseDocument(
        title=title,
        description=meta.get("description", ""),
        intro="\n".join(intro).strip(),
        exercises=exercises,
    )
    return ParseResult(document=document, problems=problems)


def _seconds(text: str) -> float | None:
    # `_` is a Python spelling (`5_0`) that the prototype's JavaScript refused.
    if "_" in text:
        return None
    try:
        value = float(_SECONDS.sub("", text.replace(",", ".")))
    except ValueError:
        return None
    return value if 0 < value <= MAX_TIME_S else None
