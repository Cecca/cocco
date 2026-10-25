"""C exercises, end to end: the same judge path as Python, with a compiler in front.

Nothing here is C-specific in the judge: `Language.compile` then
`Language.run`. These tests are the evidence that adding a language really is
a `languages.toml` entry.
"""

import textwrap
from pathlib import Path

import pytest

from cocco import model as cm
from cocco.exercises import format as exercise_format
from cocco.test_exercise_judge import _build, _check, _run, _set, _version_row

C_SET = textwrap.dedent(
    """\
    ---
    title: Pointers
    language: c
    ---

    # Hello

    Say hello.

    ```c solution
    #include <stdio.h>
    int main(void) {
        printf("hello\\n");
        return 0;
    }
    ```

    ```c starter
    int main(void) {
        return 0;
    }
    ```

    ```input
    ```

    # Divide

    Divide the first number by the second.

    ```c solution
    #include <stdio.h>
    int main(void) {
        int a, b;
        scanf("%d %d", &a, &b);
        printf("%d\\n", a / b);
        return 0;
    }
    ```

    ```input
    6 3
    ```

    ```input
    1 0
    ```

    ```error
    SIGFPE
    ```

    # Quick

    Print ok, and be quick about it.

    time: 1

    ```c solution
    #include <stdio.h>
    int main(void) { puts("ok"); return 0; }
    ```
    """
)

HELLO = """\
#include <stdio.h>
int main(void) {
    printf("hello\\n");
    return 0;
}
"""


def test_a_c_set_parses():
    parsed = exercise_format.parse(C_SET, default_title="x")
    assert parsed.problems == []
    assert parsed.document.language == "c"


async def _built_c(db, app_engine) -> tuple[int, int]:
    user_id, set_id = _set(db)
    version_id, _ = await _build(db, app_engine, user_id, set_id, C_SET)
    version = _version_row(db, version_id)
    assert version.status == cm.ExerciseVersionStatus.ready, version.problems
    assert version.document["language"] == "c"
    return user_id, version_id


@pytest.mark.integration
async def test_a_c_build_computes_expected_outputs(db, app_engine):
    user_id, set_id = _set(db)
    version_id, _ = await _build(db, app_engine, user_id, set_id, C_SET)
    version = _version_row(db, version_id)
    hello, divide, _ = version.document["exercises"]
    assert version.problems == []
    assert hello["tests"][0]["expected_output"] == "hello\n"
    assert [(t["expected_output"], t["expected_error"]) for t in divide["tests"]] == [
        ("2\n", None),
        ("", "SIGFPE"),
    ]


@pytest.mark.integration
async def test_hello_world_in_c_passes_a_check(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    result = await _check(db, app_engine, user_id, version_id, 1, HELLO)
    assert result.passed
    assert [t.verdict for t in result.tests] == ["pass"]
    assert result.compile is not None and result.compile.ok


@pytest.mark.integration
async def test_a_compile_error_is_a_check_without_tests(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    result = await _check(
        db, app_engine, user_id, version_id, 1,
        "int main(void) {\n    return undeclared;\n}\n",
    )
    assert (result.passed, result.tests) == (False, [])
    assert result.compile is not None and not result.compile.ok
    assert "main.c:2" in result.compile.diagnostics


@pytest.mark.integration
async def test_a_warning_keeps_the_compile_ok(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    result = await _check(
        db, app_engine, user_id, version_id, 1,
        '#include <stdio.h>\nint main(void) {\n    int unused;\n    printf("hello\\n");\n    return 0;\n}\n',
    )
    assert result.passed
    assert result.compile.ok
    assert "unused" in result.compile.diagnostics
    assert "main.c:3" in result.compile.diagnostics


DIVIDE = """\
#include <stdio.h>
int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\\n", a / b);
    return 0;
}
"""


@pytest.mark.integration
async def test_division_by_zero_is_sigfpe(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    result = await _check(db, app_engine, user_id, version_id, 2, DIVIDE)
    assert result.passed
    crashed = result.tests[1]
    assert (crashed.error, crashed.exit) == ("SIGFPE", 128 + 8)

    # Guarding the division is a different failure: no error where one is expected.
    guarded = await _check(
        db, app_engine, user_id, version_id, 2, DIVIDE.replace("a / b", "b ? a / b : 0")
    )
    assert [t.verdict for t in guarded.tests] == ["pass", "noerror"]


@pytest.mark.integration
async def test_a_failed_assert_is_sigabrt(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    run = await _run(
        db, app_engine, user_id, version_id,
        "#include <assert.h>\nint main(void) {\n    int c = 3;\n    assert(c > 5);\n    return 0;\n}\n",
        "",
    )
    assert (run.status, run.error, run.exit) == ("ok", "SIGABRT", 128 + 6)
    assert "Assertion" in run.stderr
    assert "c>5" in run.stderr.replace(" ", "")
    assert run.compile.ok


@pytest.mark.integration
async def test_a_c_program_that_never_ends_times_out(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    result = await _check(
        db, app_engine, user_id, version_id, 3,
        "int main(void) {\n    for (;;) {}\n}\n",
    )
    assert [t.verdict for t in result.tests] == ["timeout"]
    assert not result.passed


@pytest.mark.integration
async def test_a_c_flood_is_truncated(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    result = await _check(
        db, app_engine, user_id, version_id, 3,
        '#include <stdio.h>\nint main(void) {\n    for (;;) puts("xxxxxxxxxxxxxxxx");\n}\n',
    )
    [test] = result.tests
    assert test.verdict == "truncated"
    assert test.truncated


@pytest.mark.integration
async def test_a_nonzero_exit_is_a_failure_without_a_name(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    result = await _check(
        db, app_engine, user_id, version_id, 1,
        '#include <stdio.h>\nint main(void) {\n    printf("hello\\n");\n    return 3;\n}\n',
    )
    [test] = result.tests
    assert (test.verdict, test.exit, test.error) == ("error", 3, None)


@pytest.mark.integration
@pytest.mark.parametrize("host_file", ["/etc/passwd", str(Path(__file__).parents[1] / "pyproject.toml")])
async def test_the_compiler_cannot_read_the_judge_host(db, app_engine, host_file):
    user_id, version_id = await _built_c(db, app_engine)
    source = f'#include "{host_file}"\nint main(void) {{ return 0; }}\n'
    result = await _check(db, app_engine, user_id, version_id, 1, source)
    assert (result.passed, result.tests) == (False, [])
    assert not result.compile.ok
    assert "main.c:1" in result.compile.diagnostics


@pytest.mark.integration
async def test_a_c_run_shows_stdout_and_a_failed_compile(db, app_engine):
    user_id, version_id = await _built_c(db, app_engine)
    run = await _run(
        db, app_engine, user_id, version_id,
        '#include <stdio.h>\nint main(void) {\n    int n;\n    scanf("%d", &n);\n    printf("n=%d\\n", n);\n    return 0;\n}\n',
        "7\n",
    )
    assert (run.status, run.exit, run.error) == ("ok", 0, None)
    assert run.stdout == "n=7\n"
    assert run.compile.ok

    broken = await _run(db, app_engine, user_id, version_id, "int main(void) { nope }\n", "")
    assert broken.compile is not None and not broken.compile.ok
    assert "main.c:1" in broken.compile.diagnostics
    assert broken.stdout == ""


@pytest.mark.integration
async def test_libfdi_h_is_on_the_include_path(db, app_engine):
    """The flake's gcc ships `libfdi.h`, so a program includes it with nothing
    beside it in the box - and `-isystem` keeps `-Wall` quiet about it."""
    user_id, version_id = await _built_c(db, app_engine)
    run = await _run(
        db, app_engine, user_id, version_id,
        '#include "libfdi.h"\nint main(void) {\n    int n = input_int();\n    printf("%d\\n", 2 * n);\n    return 0;\n}\n',
        "21\n",
    )
    assert run.compile.ok, run.compile.diagnostics
    assert run.compile.diagnostics == ""
    assert (run.status, run.exit, run.stdout) == ("ok", 0, "42\n")


def _source(solution: str, starter: str | None = None, error: str | None = None) -> str:
    text = (
        "---\ntitle: T\nlanguage: c\n---\n\n# One\n\nDo it.\n\n"
        f"```c solution\n{solution}```\n\n"
    )
    if starter is not None:
        text += f"```c starter\n{starter}```\n\n"
    text += "```input\n```\n"
    if error is not None:
        text += f"\n```error\n{error}\n```\n"
    return text


@pytest.mark.integration
async def test_a_solution_that_does_not_compile_fails_the_build(db, app_engine):
    user_id, set_id = _set(db)
    version_id, _ = await _build(
        db, app_engine, user_id, set_id, _source("int main(void) { return x; }\n")
    )
    version = _version_row(db, version_id)
    assert version.status == cm.ExerciseVersionStatus.failed
    [problem] = version.problems
    assert (problem["level"], problem["text"]) == ("error", "The solution does not compile.")
    assert "main.c:1" in problem["detail"]


@pytest.mark.integration
async def test_a_starter_that_does_not_compile_is_a_warning(db, app_engine):
    user_id, set_id = _set(db)
    version_id, _ = await _build(
        db, app_engine, user_id, set_id,
        _source(HELLO, starter="int main(void) { /* fill me in */\n"),
    )
    version = _version_row(db, version_id)
    assert version.status == cm.ExerciseVersionStatus.ready, version.problems
    [problem] = version.problems
    assert (problem["level"], problem["text"]) == (
        "warning", "Students start from code that does not compile."
    )
    assert "main.c" in problem["detail"]


@pytest.mark.integration
async def test_a_python_error_name_in_a_c_file_names_the_signal(db, app_engine):
    user_id, set_id = _set(db)
    crash = "int main(void) {\n    volatile int *p = 0;\n    return *p;\n}\n"
    version_id, _ = await _build(
        db, app_engine, user_id, set_id, _source(crash, error="ValueError")
    )
    version = _version_row(db, version_id)
    assert version.status == cm.ExerciseVersionStatus.failed
    [problem] = version.problems
    assert problem["level"] == "error"
    assert "SIGSEGV" in problem["text"] and "ValueError" in problem["text"]
