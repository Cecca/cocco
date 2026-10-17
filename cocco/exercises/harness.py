"""Run a student's Python program and report what it did, as one JSON object.

    python harness.py <check|run> main.py

This file is not imported by cocco: the judge copies it into an isolate box
next to the student's `main.py` and runs it there, so it is plain stdlib and
self-contained. It is the port of the `RUNNER` in the prototype's
`py-worker.js`, and it exists because two of the exercise semantics are about
Python itself rather than about bytes on stdout:

- prompts written by `input("...")` are not output. In `check` mode they are
  dropped, so a student may print them or not; in `run` mode the prompt *and the
  line read* are echoed, so the transcript of a non-interactive run reads like
  a terminal session.
- an `error: ValueError` test passes on that exception or a subclass, which
  takes the exception's MRO, which only the process that raised it can see.

Whatever the program does, the harness exits 0 and writes one JSON object, on
one line, on the real stdout - `schema.HarnessReport` is its shape:

    {"stdout": "...", "stderr": "...", "truncated": false, "exit": 0,
     "exception": {"type": "ValueError", "mro": ["ValueError", "Exception",
                   "BaseException", "object"], "line": 3,
                   "traceback": "Traceback (most recent call last): ..."}}

`exception` is null when the program did not end with one; `exit` is the code
a `sys.exit()` asked for, as an int, and 0 otherwise. The program's stdout and
stderr are buffered here, each capped at `LIMIT` characters, and so is the
traceback: a program that prints endlessly is stopped at the cap with
`truncated` set, rather than filling memory.

The isolate status says how the run ended; this JSON says what the program did.
A timeout or an OOM produces no JSON at all, and that is the judge's to report.
Two things the program can do to the report, and how the judge reads them:

- it can write to fd 1 directly, ahead of the report or in place of it. The
  judge reads the **last line** of stdout. A forgery is accepted: the output is
  still compared by the judge, so it can only fake the exception check, and
  exercises grade nothing.
- it can end the process before the report is written - `os._exit(0)`, or
  `os.closerange` taking the harness's own copy of fd 1 with it. Status `OK`
  with no report is therefore *something the program did* ("ended without
  reporting"), never a judge internal error.
"""

import builtins
import io
import json
import os
import sys
import traceback
import types

LIMIT = 256 * 1024
MODES = ("check", "run")
# The widest exit code reported as asked; anything past it - `sys.exit(10**5000)`
# included, which json could not even write - is reported as 1.
MAX_EXIT = 2**31 - 1


class OutputLimit(BaseException):
    """Raised into the program by a write past `LIMIT`.

    A `BaseException`, so that the `except Exception` a student wrote around
    their loop does not swallow it.
    """


class Capture(io.TextIOBase):
    """A text stream that keeps what is written to it, up to the limit."""

    def __init__(self, name, limit):
        self._name = name
        self._limit = limit
        self._parts = []
        self._size = 0
        self.truncated = False

    encoding = "utf-8"

    @property
    def name(self):
        return "<" + self._name + ">"

    def writable(self):
        return True

    def isatty(self):
        return False

    def flush(self):
        pass

    def write(self, s):
        if not isinstance(s, str):
            raise TypeError("write() argument must be str, not " + type(s).__name__)
        room = self._limit - self._size
        if len(s) > room:
            # Keep what fits, so the student sees how the flood began.
            if room:
                self._parts.append(s[:room])
            self._size = self._limit
            self.truncated = True
            raise OutputLimit()
        self._parts.append(s)
        self._size += len(s)
        return len(s)

    def getvalue(self):
        return "".join(self._parts)


def make_input(mode):
    """The `input` the program sees: prompts dropped in check, echoed in run."""

    def harness_input(prompt=""):
        if mode == "run":
            sys.stdout.write(str(prompt))
        line = sys.stdin.readline()
        if not line:
            raise EOFError("EOF when reading a line")
        if line.endswith("\n"):
            line = line[:-1]
        if mode == "run":
            sys.stdout.write(line + "\n")
        return line

    return harness_input


def _student_frames_only(te, path, seen):
    """Drop every frame not in the student's file, chained exceptions included."""
    if te is None or id(te) in seen:
        return
    seen.add(id(te))
    te.stack = traceback.StackSummary.from_list([f for f in te.stack if f.filename == path])
    _student_frames_only(te.__cause__, path, seen)
    _student_frames_only(te.__context__, path, seen)
    for inner in getattr(te, "exceptions", None) or ():
        _student_frames_only(inner, path, seen)


def _line(exc, path):
    # The deepest frame in the student's file is the line to point at. A
    # SyntaxError at compile time has no such frame and carries its own.
    line = getattr(exc, "lineno", None) if isinstance(exc, SyntaxError) else None
    t = exc.__traceback__
    while t is not None:
        if t.tb_frame.f_code.co_filename == path:
            line = t.tb_lineno
        t = t.tb_next
    # `lineno` is an attribute a student's own SyntaxError can set to anything.
    return line if type(line) is int else None


def describe(exc, path):
    """The `exception` object of the report.

    Every step is guarded: this runs on an exception a student chose, whose
    `__str__` may raise and whose message may be ten megabytes long.
    """
    try:
        line = _line(exc, path)
    except BaseException:
        line = None
    try:
        # Only the student's frames: this harness's own, the `exec` and the
        # `input` wrapper, are noise to someone learning to read a traceback.
        te = traceback.TracebackException(type(exc), exc, exc.__traceback__)
        _student_frames_only(te, path, set())
        text = "".join(te.format())
    except BaseException:
        text = type(exc).__name__ + "\n"
    return {
        "type": type(exc).__name__,
        "mro": [c.__name__ for c in type(exc).__mro__],
        "line": line,
        "traceback": text[:LIMIT],
    }


def _exit_code(code, stderr):
    """What `sys.exit(code)` reports, always an int, as `python` would decide it."""
    if code is None:
        return 0
    if isinstance(code, int):
        # A bool is an int, and `true` in the report would not be.
        return int(code) if -MAX_EXIT - 1 <= code <= MAX_EXIT else 1
    try:
        print(code, file=stderr)
    except BaseException:
        # A full stderr, or an object whose `__str__` raises.
        pass
    return 1


def run(mode, path, stdin, restore=True):
    """Run the program at `path` on `stdin` (bytes) and return the report, as a dict.

    `restore` puts the interpreter back as it was, for a caller that goes on
    using it. `main` passes False: it ends with `os._exit` anyway, and leaving
    the captures in place is what keeps a thread the program left running from
    printing onto fd 1 next to the report.
    """
    out = Capture("stdout", LIMIT)
    err = Capture("stderr", LIMIT)
    report = {"exit": 0, "exception": None}
    # Read outside the `try`: a missing main.py is the judge's mistake, and
    # must surface as a harness failure rather than as the student's exception.
    with open(path, "rb") as f:
        source = f.read()
    # As `runpy` does it: a real module, installed as `__main__`, so that what
    # looks the program up by that name - pickle, `get_type_hints`, a
    # dataclass's string annotations - finds the program and not this harness.
    module = types.ModuleType("__main__")
    module.__file__ = path
    module.__dict__["__builtins__"] = builtins
    saved = (
        sys.stdin, sys.stdout, sys.stderr, sys.argv, builtins.input,
        sys.modules["__main__"],
    )
    # A text wrapper over bytes rather than a StringIO, so that
    # `sys.stdin.buffer` is there for a program that reads it.
    sys.stdin = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8", errors="replace")
    sys.stdout, sys.stderr = out, err
    sys.argv = [path]
    builtins.input = make_input(mode)
    sys.modules["__main__"] = module
    try:
        # Bytes, so that compile() honours a coding cookie and a BOM the way
        # `python main.py` would.
        code = compile(source, path, "exec")
        exec(code, module.__dict__)
    except OutputLimit:
        pass
    except SystemExit as e:
        report["exit"] = _exit_code(e.code, err)
    except BaseException as e:
        report["exception"] = describe(e, path)
    finally:
        if restore:
            (
                sys.stdin, sys.stdout, sys.stderr, sys.argv, builtins.input,
                sys.modules["__main__"],
            ) = saved
    report["stdout"] = out.getvalue()
    report["stderr"] = err.getvalue()
    report["truncated"] = out.truncated or err.truncated
    return report


def main(argv):
    if len(argv) != 3 or argv[1] not in MODES:
        sys.stderr.write("usage: python harness.py <check|run> main.py\n")
        return 2
    # A private copy of fd 1, taken before the program runs, so the report
    # still has somewhere to go if the program closes or redirects its stdout.
    real_stdout = os.fdopen(os.dup(1), "wb")
    stdin = sys.stdin.buffer.read()
    report = run(argv[1], argv[2], stdin, restore=False)
    # ASCII, so the bytes do not depend on whatever locale the box has. The
    # leading newline ends whatever the program wrote to fd 1 unterminated, so
    # the report is always a line of its own - the last one.
    real_stdout.write(b"\n" + json.dumps(report, ensure_ascii=True).encode("ascii") + b"\n")
    real_stdout.flush()
    # Not `return 0`: a thread the program left running, or an atexit hook it
    # registered, must not get to keep the box alive or print after the report.
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
