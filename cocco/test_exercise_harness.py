"""The in-box harness, run as the program it is.

`cocco/exercises/harness.py` is never imported by cocco: the judge copies it
into an isolate box and runs `python harness.py <mode> main.py` there. So it is
exercised the same way here, as a subprocess with the student's file beside it,
minus isolate. That the sandbox can run it too is the judge's integration tests'
business.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cocco.exercises import HARNESS_PATH
from cocco.exercises import harness
from cocco.schema import HarnessReport


def run_raw(tmp_path: Path, code: str, stdin: str = "", mode: str = "check"):
    (tmp_path / "main.py").write_text(code, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(HARNESS_PATH), mode, "main.py"],
        input=stdin.encode(),
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )


def run_harness(tmp_path: Path, code: str, stdin: str = "", mode: str = "check") -> dict:
    proc = run_raw(tmp_path, code, stdin, mode)
    assert proc.returncode == 0, proc.stderr
    # The report is the last line, and the judge reads nothing else.
    last = proc.stdout.decode("ascii").splitlines()[-1]
    report = json.loads(last)
    HarnessReport.model_validate(report)
    return report


def test_the_report_is_the_only_thing_on_stdout(tmp_path):
    proc = run_raw(tmp_path, "print('hi')\n")
    assert proc.stdout.startswith(b"\n{")
    assert proc.stdout.count(b"\n") == 2


def test_the_harness_ships_inside_the_package():
    assert HARNESS_PATH.is_file()
    assert HARNESS_PATH.parent == Path(harness.__file__).resolve().parent


def test_a_clean_run(tmp_path):
    report = run_harness(tmp_path, "import sys\nprint('hi')\nprint('oops', file=sys.stderr)\n")
    assert report == {
        "stdout": "hi\n",
        "stderr": "oops\n",
        "truncated": False,
        "exit": 0,
        "exception": None,
    }


def test_check_mode_drops_prompts(tmp_path):
    code = "a = input('First? ')\nb = input('Second? ')\nprint(int(a) + int(b))\n"
    report = run_harness(tmp_path, code, stdin="3\n4\n", mode="check")
    assert report["stdout"] == "7\n"


def test_run_mode_echoes_the_prompt_and_the_line_read(tmp_path):
    code = "a = input('First? ')\nb = input()\nprint(int(a) + int(b))\n"
    # The last line has no newline, and the echo still ends one.
    report = run_harness(tmp_path, code, stdin="3\n4", mode="run")
    assert report["stdout"] == "First? 3\n4\n7\n"


def test_input_past_the_end_is_an_eof_error(tmp_path):
    report = run_harness(tmp_path, "input()\ninput()\n", stdin="only one\n")
    exc = report["exception"]
    assert exc["type"] == "EOFError"
    assert exc["line"] == 2


def test_the_exception_carries_its_mro_line_and_a_traceback_of_the_students_frames(tmp_path):
    code = (
        "class Negative(ValueError):\n"
        "    pass\n"
        "\n"
        "def check(n):\n"
        "    if n < 0:\n"
        "        raise Negative(n)\n"
        "\n"
        "print('before')\n"
        "check(int(input()))\n"
    )
    report = run_harness(tmp_path, code, stdin="-3\n")
    assert report["stdout"] == "before\n"
    exc = report["exception"]
    assert exc["type"] == "Negative"
    assert exc["mro"] == ["Negative", "ValueError", "Exception", "BaseException", "object"]
    validated = HarnessReport.model_validate(report).exception
    assert validated is not None
    assert validated.classes[1] == "ValueError"
    assert exc["line"] == 6
    tb = exc["traceback"]
    assert 'File "main.py", line 9, in <module>' in tb
    assert 'File "main.py", line 6, in check' in tb
    assert "harness" not in tb
    assert tb.rstrip().endswith("Negative: -3")
    # The traceback is reported, not written to the program's stderr.
    assert report["stderr"] == ""


def test_a_syntax_error_points_at_its_own_line(tmp_path):
    report = run_harness(tmp_path, "x = 1\nprint(x\n")
    exc = report["exception"]
    assert exc["type"] == "SyntaxError"
    assert exc["line"] == 2
    assert "main.py" in exc["traceback"]
    assert "harness" not in exc["traceback"]


def test_an_exception_inside_input_points_at_the_students_line(tmp_path):
    report = run_harness(tmp_path, "x = 1\ny = input()\n", stdin="")
    assert report["exception"]["line"] == 2
    assert "harness" not in report["exception"]["traceback"]


@pytest.mark.parametrize(
    "code, exit_code, stderr",
    [
        ("import sys\nsys.exit()\n", 0, ""),
        ("import sys\nsys.exit(3)\n", 3, ""),
        ("import sys\nsys.exit('bye')\n", 1, "bye\n"),
        ("import sys\nsys.exit(True)\n", 1, ""),
        ("import sys\nsys.exit(False)\n", 0, ""),
        ("import sys\nsys.exit(10**5000)\n", 1, ""),
        (
            "import sys\nclass X:\n    def __str__(self):\n        raise RuntimeError\n"
            "sys.exit(X())\n",
            1,
            "",
        ),
    ],
)
def test_sys_exit_is_an_exit_code_not_an_exception(tmp_path, code, exit_code, stderr):
    report = run_harness(tmp_path, code)
    assert report["exception"] is None
    assert report["exit"] == exit_code
    assert type(report["exit"]) is int
    assert report["stderr"] == stderr


def test_an_endless_print_stops_at_the_cap(tmp_path):
    report = run_harness(tmp_path, "while True:\n    print('spam')\n")
    assert report["truncated"] is True
    assert len(report["stdout"]) == harness.LIMIT
    assert report["stdout"].startswith("spam\nspam\n")
    assert report["exception"] is None


def test_catching_the_cap_does_not_hide_it(tmp_path):
    code = (
        "try:\n"
        "    while True:\n"
        "        print('x' * 1000)\n"
        "except BaseException:\n"
        "    pass\n"
        "print('after')\n"
    )
    report = run_harness(tmp_path, code)
    assert report["truncated"] is True
    assert report["exception"] is None


def test_stderr_has_its_own_cap(tmp_path):
    report = run_harness(tmp_path, "import sys\nwhile True:\n    sys.stderr.write('e' * 4096)\n")
    assert report["truncated"] is True
    assert report["stdout"] == ""
    assert len(report["stderr"]) == harness.LIMIT


def test_the_program_runs_as_main_with_its_own_argv(tmp_path):
    code = "import sys\nif __name__ == '__main__':\n    print(sys.argv, __file__)\n"
    assert run_harness(tmp_path, code)["stdout"] == "['main.py'] main.py\n"


def test_non_ascii_round_trips(tmp_path):
    report = run_harness(tmp_path, "print(input()[::-1])\n", stdin="perché ✓\n")
    assert report["stdout"] == "✓ éhcrep\n"


def test_closing_stdout_does_not_lose_the_report(tmp_path):
    report = run_harness(tmp_path, "import os\nprint('kept')\nos.close(1)\n")
    assert report["stdout"] == "kept\n"
    assert report["exception"] is None


def test_a_leftover_thread_does_not_keep_the_harness_alive(tmp_path):
    code = (
        "import threading, time\n"
        "threading.Thread(target=time.sleep, args=(60,)).start()\n"
        "print('done')\n"
    )
    assert run_harness(tmp_path, code)["stdout"] == "done\n"


def test_a_bad_command_line_is_a_usage_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(HARNESS_PATH), "grade", "main.py"],
        capture_output=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 2
    assert proc.stdout == b""


def test_harness_frames_are_dropped_from_chained_tracebacks_too(tmp_path):
    code = (
        "try:\n"
        "    input()\n"
        "except EOFError:\n"
        "    raise ValueError('no input')\n"
    )
    exc = run_harness(tmp_path, code)["exception"]
    assert exc["type"] == "ValueError"
    assert exc["line"] == 4
    assert "During handling of the above exception" in exc["traceback"]
    assert "EOFError" in exc["traceback"]
    assert "harness" not in exc["traceback"]


def test_harness_frames_are_dropped_from_exception_groups(tmp_path):
    code = (
        "def read():\n"
        "    try:\n"
        "        input()\n"
        "    except EOFError as e:\n"
        "        return e\n"
        "raise ExceptionGroup('many', [read(), ValueError(1)])\n"
    )
    exc = run_harness(tmp_path, code)["exception"]
    assert exc["type"] == "ExceptionGroup"
    assert "EOFError" in exc["traceback"]
    assert "harness" not in exc["traceback"]


def test_a_huge_exception_message_is_capped(tmp_path):
    exc = run_harness(tmp_path, "raise ValueError('x' * 10_000_000)\n")["exception"]
    assert exc["type"] == "ValueError"
    assert len(exc["traceback"]) == harness.LIMIT


def test_a_class_defined_by_the_program_pickles(tmp_path):
    code = (
        '"""The docstring."""\n'
        "import pickle\n"
        "from dataclasses import dataclass\n"
        "from typing import get_type_hints\n"
        "@dataclass\n"
        "class Point:\n"
        "    x: 'int'\n"
        "print(pickle.loads(pickle.dumps(Point(3))))\n"
        "print(get_type_hints(Point))\n"
        "print(__doc__)\n"
    )
    report = run_harness(tmp_path, code)
    assert report["exception"] is None
    assert report["stdout"] == "Point(x=3)\n{'x': <class 'int'>}\nThe docstring.\n"


def test_stdin_has_a_buffer(tmp_path):
    report = run_harness(tmp_path, "import sys\nprint(sys.stdin.buffer.read())\n", stdin="ab\n")
    assert report["stdout"] == "b'ab\\n'\n"


def test_writing_to_fd_1_directly_still_leaves_the_report_last(tmp_path):
    report = run_harness(tmp_path, "import os\nos.write(1, b'forged, unterminated')\nprint('ok')\n")
    assert report["stdout"] == "ok\n"


def test_a_leftover_thread_cannot_print_next_to_the_report(tmp_path):
    code = (
        "import threading\n"
        "def spam():\n"
        "    while True:\n"
        "        print('late')\n"
        "threading.Thread(target=spam, daemon=True).start()\n"
    )
    proc = run_raw(tmp_path, code)
    # The thread is still printing while the report is written, into the
    # captures it found in place rather than onto fd 1.
    assert proc.stdout.splitlines()[0] == b""
    assert len(proc.stdout.splitlines()) == 2


def test_the_program_can_end_the_process_before_the_report(tmp_path):
    # Something the program did, which the judge reports as such: there is no
    # way to write a report from a process that has been told to stop.
    proc = run_raw(tmp_path, "import os\nos._exit(0)\n")
    assert proc.returncode == 0
    assert proc.stdout == b""
