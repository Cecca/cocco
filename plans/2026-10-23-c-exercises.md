---
status: done
---

# Exercises in any language from `languages.toml` (C first)

## Goal

A teacher can write an exercise set in any language `languages.toml` knows, run it live in class exactly as a
Python set is run today, and students get the same Run / Check loop. C is the first new language, with compiler
diagnostics where Python shows a `SyntaxError`. Everything above the execution layer stays as it is: sets, versions,
sessions, pacing, progress, the dashboard, the projector, transfer. A non-Python set is the same `.md` file with one
more front-matter line.

**The bar is the challenge judge's: adding a language is a `languages.toml` entry.** Challenges know two kinds of
language, interpreted and compiled, and nothing else: `Language.compile` (a no-op when interpreted) then
`Language.run`. Exercises get the same two calls and no per-language code. Anything that genuinely differs between
languages (how an uncaught error names itself on stderr, the editor mode) is a *key* in `languages.toml`, not a
branch in Python.

That means **`harness.py` goes**. Every program, Python included, runs as `Language.run` runs it: `python main.py`,
or the compiled binary. Two Python-only semantics go with it, deliberately:

- **Prompts are output.** `input("n? ")` writes `n? ` to stdout like any other print. Students are told not to pass a
  prompt to `input()` in exercises (the format docstring, the guide and a line on the exercise page say so). The Run
  tab no longer echoes the lines read; it shows the stdin given above the transcript, for every language.
- **`error:` matches a name, not a class hierarchy.** `error: LookupError` no longer passes on an `IndexError`. See
  "Errors" below.

Every stored document, job result and problem list still validates and renders. What changes for Python is how a new
check or build runs.

## Where things stand (as of `c151886`)

- `schema.ExerciseDocument.language` exists, defaults to `"python"`; `query_job_exercise` already reads
  `document->>'language'` and hands it to the judge.
- `judge._python(language_name)` resolves it against `languages.toml` and refuses anything not interpreted.
- `Language.compile` already compiles in its own isolate box (`2026-10-23-compile-inside-isolate.md`, landed
  `5e377ed`): box-relative placeholders, `collect` / `collect_into`, the `COMPILE_*` budget, and the outcome mapping
  (`TO`, then `oom`, then `RE`, then anything else, then a missing artifact) into a `CompileOutcome`. So
  `#include "/etc/passwd"` is already "no such file", for exercises too, once they call it.
- `Language.run` runs an interpreter on the source or the compiled binary, in a box.
- `[c]` is in `languages.toml`, so gcc is built and prewarmed at judge startup.

Python is hardwired here, and every row is something this plan removes or turns into data:

| Where | What assumes Python | Becomes |
|---|---|---|
| `exercises/harness.py`, `schema.HarnessReport` | the execution model: prompts, exceptions with an MRO, line, traceback | deleted; `Language.run` |
| `judge.run_in_harness`, `read_execution`, `_MAIN_IN_BOX`, `_HARNESS_IN_BOX` | `python harness.py <mode> main.py`; `no_report` | `run_exercise_program`, `read_exercise_run`; `main.<ext>` |
| `judge.EXERCISE_ENV` | `PYTHONHASHSEED` | `env` key on `[python]` |
| `judge._python` | "only interpreted languages" | `exercise_language`: any language |
| `judge.build_exercise` | writes `solution.py` | a subdirectory per program |
| `exercises/format.py` | fence prefix `python` / `py`; `error` must be a Python identifier | prefix = the language's name or extensions; `error` is one word |
| `exercises/hints.py` | keyed on Python exception names | unchanged, plus signal names |
| `web/exercise_sets._builtin_exception_name` | redacts to the nearest Python builtin | redacts to a name `HINTS` knows |
| `static/exercise.js`, `_exercise-editor-head.jinja` | CodeMirror `mode: "python"`, only `python.js` loaded | `editor_mode` key, every vendored mode loaded |
| `exercise.jinja`, `f-exercise-drill-in.jinja`, `exercise-projector.jinja` | `highlight("python")` | `highlight(language)` |
| `f-exercise-job.jinja` | "traceback", `error_hint(type, line)`, no stdin shown | "error output", no line, stdin shown on Run |

## Design decisions

### The language is a front-matter key, checked against `languages.toml`

```markdown
---
title: Pointers
language: c
---
```

`FRONT_MATTER_KEYS` gains `language`. Absent means `python`, so every existing file means what it meant. The value is
checked against the keys of `languages.toml`, and anything else is a parse error listing them, at import like every
other parse problem (a C file read as Python would build every solution into a `SyntaxError`, and a build is the
slow place to find that out).

`format.py` is tested by import and touches no judge, so `load_languages` and `LANGUAGES_PATH` move out of
`judge.py` into a small `cocco/languages.py` (the `Language` dataclass stays where `compile` / `run` are, or moves
with it if the import graph is cleaner that way; step 2 decides). `format.py` only needs the names and extensions,
and the web process needs `editor_mode`; neither should import the judge.

**Fence prefixes come from the entry, not a list in `format.py`**: a block may be prefixed by the language's name or
any of its `extensions`. For `[python]` (`extensions = ["py"]`) that is `python starter` / `py starter`, exactly
today's spelling; for `[c]` it is `c starter`; for `[cpp]`, `cpp` / `cc` / `cxx`. A block prefixed with *another*
known language's name or extension (`python solution` in a C file) is an error naming the right spelling, for the
reason `_ITALIAN` exists: left alone it would be "any other code block", which is part of the statement, solution and
all. `linguaggio` joins `_ITALIAN`.

The compiled document keeps `format: 1`. `language` was always in it; only its set of values grows.

### One exercise path, the challenge's two calls

`_python` becomes `exercise_language(name) -> Language`, which refuses only a name `languages.toml` does not have
(the parse already did; this is the judge not trusting a stored document). Then:

```python
async def prepare_exercise_program(language, source, workdir) -> CompileOutcome:
    """Write `source` as `workdir / main.<ext>` and `language.compile` it."""

async def run_exercise_program(language, executable, stdin, time_limit, workdir) -> ExerciseExecution:
    """`language.run` on one stdin, read into an `ExerciseExecution` by `read_exercise_run`."""
```

That is the whole per-language surface, and it has no `if language == …`. For an interpreted language `compile`
already returns the source as the executable with `success=True`; for a compiled one it is the box compile that
landed. `check_exercise` prepares once and runs every test on the result (one compile plus N runs, never N
compiles); `build_exercise` prepares the solution once, for both of its rounds, and the starter once, each in its own
subdirectory of the job's `workdir` (the hardcoded `solution.py` goes).

**The source is `main.<extensions[0]>`**, the name diagnostics and tracebacks report against, which is the student's
file as far as they are concerned (`main.py`, `main.c`). Java would need `Main.java` for `java Main`; that is a
problem for whoever adds Java to exercises (a `source_stem` key on the entry, say), not something to build now.

**Limits.** Runs: `exercise.time_s` and `EXERCISE_MEM_LIMIT_KB`, processes as `Language.run` already computes them
(`1 + additional_processes`). Compiles: `Language.compile`'s own `COMPILE_*` budget, unchanged; there are no
`EXERCISE_COMPILE_*` constants, for the reason the earlier draft gave (gcc needs 4 processes and a few hundred MB, and
a second set of constants is a second place for the paths to disagree).

**`Language.run` and `Language.compile` gain what exercises need, for both paths:**

- `env`: a per-language `env` table in `languages.toml`, set in the run box. `[python]` gets
  `env = { PYTHONHASHSEED = "0" }`, which is what `EXERCISE_ENV` did: a `set` of strings prints in the same order in
  the solution and in the student's program, and in the build's two rounds. Challenges get it too, which only makes a
  run reproducible.
- `fsize_kb`: `run` passes it through to `isolate_run` (exercises cap output, below).

**Compiler flags are the entry's, the same for challenges and exercises**: `[c]` becomes
`["-O2", "-std=c17", "-Wall", "-o", "{output}", "{source}", "-lm"]`. There is no second `compiler_args` for exercises
(the earlier draft's `-Og -Wall -Wextra`), since that is exactly the special case this plan avoids. `-Wall` means
challenge compiles produce warnings too, which is harmless: a warning does not fail a compile, and a challenge shows a
compile's output only when it failed (step 2 confirms). `-lm` goes last, after the source, where the linker needs it;
`math.h` is the first thing a C exercise reaches for.

### Reading a run: isolate's outcome, plus one regex per language

With no harness there is no report: what the program did is isolate's status, exit code, signal, stdout and stderr.
`read_exercise_run(res, language)`:

| isolate | `ExerciseExecution` |
|---|---|
| `TO` | `timeout` |
| `oom` | `memory` |
| `XX` | raises (judge error, as today) |
| `SG` with `SIGXFSZ`, or stdout/stderr at the cap | `ok`, `truncated = True` |
| `OK` | `ok`, `exit = 0` |
| `RE` | `ok`, `exit = retcode`, `error` = the language's error name, if any |
| `SG`, any other | `ok`, `exit = 128 + signal`, `error` = the signal's name (`SIGSEGV`) |

`RunOutcome` gains `exitsig: int | None`, read from isolate's meta file, since `message` ("Caught fatal signal 11") is
prose. There is no `no_report` for new results: there is no report to forge or lose. The literal stays in
`ExerciseExecution.status` so stored results validate.

**Output cap.** The harness capped stdout and stderr at 256 K characters. isolate does it now: `EXERCISE_FSIZE_KB`
drops to the cap (256 KB), a program that writes past it dies with `SIGXFSZ`, and that is `truncated`, which is what
it was. stdout and stderr then go through `_jsonb_safe`, the same boundary the report went through, so what is
compared and what is stored stay the same bytes.

**The error name.** An exercise needs one thing the challenge judge never did: a *name* for how a program failed, for
`error:` blocks, for the hint, and for what a hidden test may show. It comes from two places, neither of them code
per language:

- a signal, which is the operating system's and the same for every language: `SIGSEGV`, `SIGFPE`, `SIGABRT` (a
  failed `assert`), …;
- otherwise, an optional `error_name` regex on the `languages.toml` entry, searched in stderr when the program exited
  non-zero, last match winning, group 1 being the name. For Python: `(?m)^([A-Za-z_][\w.]*)(?::|$)`, which takes
  `ValueError` from the last line of a traceback (`ValueError: invalid literal …`) and `SyntaxError` from a
  syntax error. A language without the key has failures that are errors with no name.

Like the harness's report, stderr is the program's to forge, and as before that can only fake the error check; it is
not graded.

`ExerciseExecution` gets `error: str | None` in place of `exception`. The traceback was only ever stderr, and now it is
in `stderr`. `line` goes: the harness was the only thing that knew it; the full stderr is shown for visible tests and
names the line itself (`main.py`, line 3 / `main.c:7`), and hidden tests withheld it anyway. Results stored before
this are read by a `mode="before"` validator that folds the old shape in: `exception.type` → `error`,
`exception.traceback` appended to `stderr`. One shape in the code, every old row still reads, **no migration**.
`HarnessException`, `HarnessReport` and `MAX_EXCEPTION_LINE` go.

### Errors: `error:` blocks name what the program dies of

```markdown
```error
ValueError
```
```

A test with an `error` block expects the program to *fail*: exit non-zero or die by a signal. A non-empty block also
names the failure, and passes when the error name above equals it. In C that is a signal (`SIGFPE`); in Python an
exception name, as today, but exactly that name: no subclasses, since there is no MRO without a harness (settled:
the loss is accepted, and nothing replaces it). An empty
block still means "whatever the solution ends with" (the build fills it in).

`format.py` checks only that the block is one word (`[A-Za-z_][\w.]*`); whether that word can come out of this
language is the build's to find out, and it already does: a solution that does not end with the declared error is a
build error ("add an error block with X"). So a C file with `error: ValueError` fails its build with a message naming
the signal the solution actually dies of.

`grade` and `error_matches` keep their shape: `error_matches(execution, expected)` is `execution.error == expected`.
An exit without a name (`return 1`, `sys.exit(1)`) is a failure with `error = None`: `error` on a test expecting a
clean run, and on a test expecting a named error, exactly as an `exit != 0` is today. Step 5 checks how an empty
`error` block is filled in by the build when the solution's failure has no name, and keeps today's behaviour.

### Hints and hidden tests key on names `HINTS` knows

`hints.py` gains the signals (`SIGSEGV`: "invalid memory access: an index past the end of an array, or a NULL or
uninitialised pointer", `SIGFPE`: "integer division by zero", `SIGABRT`: "an assert failed, or abort() was called",
…). Names cannot collide across languages in practice, so one `HINTS` dict stays. `error_hint` loses its `line`
parameter.

`_builtin_exception_name` becomes `_shown_error_name(name)`: the name if it is a key of `HINTS`, else `"error"`. That
is the same bound as before (a program choosing its own exception or signal name can only pick from a fixed list) and
it is language-neutral: a new language's errors are shown on hidden tests once `hints.py` explains them, and not
before. `exercise_check_rows` needs no language argument.

### Compile errors are a result of their own, not a verdict per test

A check that does not compile has run nothing, so N identical rows saying "compile error" would be noise. Both result
shapes gain an optional field:

```python
class ExerciseCompile(Base):
    ok: bool
    diagnostics: str     # the compiler's stderr, capped at MAX_EXERCISE_STORED_OUTPUT

class ExerciseCheckResult(Base):
    ...
    compile: ExerciseCompile | None = None   # None for an interpreted language, and for every older result

class ExerciseRunResult(...):
    compile: ExerciseCompile | None = None
```

Filled from the `CompileOutcome` whenever the language has a compiler, so it is not a C feature. A failed compile is
`passed=False, tests=[]` for a check and an empty execution for a run, with `compile.ok = False`. A successful compile
with output (warnings) keeps it, and the page shows it above the tests, collapsed. Optional with a default, so every
stored result still validates through `EXERCISE_JOB_RESULT`.

Progress needs nothing: a check that did not compile has `passed = false` and so makes the exercise *attempted*.
Diagnostics are about the student's own source and nothing else (the compile has no stdin and no hidden test), so
they are shown in full even where hidden tests are redacted.

In a build: a solution that does not compile is an **error** with the diagnostics as `detail`. A starter that does not
compile is a **warning** ("students start from code that does not compile"): a starter with a gap to fill may be
deliberate, but the teacher should know.

### A session's language is fixed

Switching a session to a newer version is refused when the new version's language differs from the pinned one's, in
the same conditional statement as the other switch guards, with a message saying so. Every student's draft, past check
and "solved" was in one language. A *set* may change language between imports, and a new session picks it up.

### Front end, driven by the entry

- `languages.toml` entries gain `editor_mode` (CodeMirror 5's mode or MIME: `"python"`, `"text/x-csrc"`,
  `"text/x-c++src"`, `"text/x-java"`). The editor's textarea carries `data-mode="{{ editor_mode }}"`, and
  `exercise.js` passes it to CodeMirror with no mapping of its own.
- Vendor CodeMirror 5's `clike.js` (C, C++, Java, …), the same release as the `codemirror.js` in
  `static/codemirror/`, MIT like the rest. `_exercise-editor-head.jinja` loads every vendored mode: a few KB, no
  per-page branching. A future language whose mode is not vendored is one more file.
- `highlight("python")` → `highlight(language)` in the three templates. The `languages.toml` key is the Pygments
  alias for every language there (`python`, `c`, `cpp`, `java`); a `pygments` key is the escape hatch if one ever is
  not, not something to add now.
- Tab still inserts spaces. The comment that justifies it is Python's and becomes "spaces, never a tab character:
  Python minds, and nothing else does".
- `f-exercise-job.jinja`: the compile block (diagnostics in the `<pre>` style the traceback used), "traceback" →
  "error output", the error name with its hint, and the Run tab shows the stdin given above the transcript.
- The exercise page says, once, near the Run tab: "Don't print a prompt when reading input: everything your program
  prints is compared."
- The set page and the session card show the language as a small badge.

### What does not change

Transfer (`cocco pull --set` / `push`): the file carries `language:`; the CLI, the `ETag` rule and the push lock are
untouched. Sessions, pacing, reveals, the dashboard, the projector, deletion, retention: untouched. The challenge path
gains only `env`, `fsize_kb` passed through and `-lm`.

**Versions already built by the harness stay as they are.** Their expected outputs were computed with prompts
dropped, and their `error` blocks may name a base class. A session pinned to one keeps working for programs without
prompts and with exact error names; a teacher who wants the new semantics re-imports, which builds a new version, and
switches. Step 9 checks the dev seed and any set in the repo for `input("…")` with a prompt and for base-class `error`
blocks.

## Spike results (step 1)

Run through `isolate_run` / `Language.compile` with the real nix gcc and python on this host (script, not committed:
the session scratchpad `spike.py`, `spike2.py`, `fpe.py`). The meta file is `/tmp/meta-<box>.txt`; only `exitsig`
needs adding to what `isolate_run` already reads.

**Signals in `--meta`** (C binary and `python` agree): a death by signal is `status:SG`, `exitsig:<n>`,
`message:Caught fatal signal <n>`, and no `exitcode`. Seen: SIGSEGV `exitsig:11`, SIGFPE `8`, SIGABRT `6` (a failed
`assert`: stderr carries `main: main.c:2: main: Assertion 'c>5' failed.`), SIGXFSZ `25`. A plain exit is
`status:RE`, `exitcode:N`, `message:Exited with error status N`; a clean exit has `exitcode:0` and **no `status`**
(already `OK` in `RunOutcome`). `RunOutcome.retcode` is useless for both (`isolate`'s own rc: 1 for RE/SG, 0 becomes
127 via `retcode or 127`), so `read_exercise_run` must take the exit code from the meta file too: **step 3 should add
`exitcode` beside `exitsig`**, not just `exitsig`. Python's `os.kill(getpid(), SIGSEGV/SIGFPE)` and `os.abort()`
are `SG` 11 / 8 / 6 as well.

**Contradicts the design in three places, none fatal:**

1. **Python ignores SIGXFSZ.** A Python flood to stdout or stderr at `--fsize=256` is **not `SG`**: it is `RE`,
   `exitcode:120` (Python's "failed to flush stdout"), with `OSError: [Errno 27] File too large` in stderr and the
   captured file exactly 262144 bytes. A C flood *is* `SG` 25, also exactly 262144 B in the flooded stream (the other
   stream empty). So the "`SG` with `SIGXFSZ`" row is only half the detection; the **"stdout or stderr at the cap"
   test is the one that works for Python and must come before the `RE` row** (else Python's flood reads as
   `error = OSError`). Cost: a program writing *exactly* 256 KB and exiting cleanly is also flagged `truncated`
   (verified: C writing exactly 262144 B is `OK`, exit 0). Accepted. The check is on bytes, but `RunOutcome` holds
   decoded `str` (`errors="replace"`); non-UTF-8 output can change the length, so compare `len(s.encode()) >= cap`
   (a replacement char is 3 bytes, so this errs towards truncated, fine) or add the byte counts to `RunOutcome`.
2. **`KeyboardInterrupt` is `SG`, signal 2.** Python re-raises SIGINT on itself after printing the traceback, so
   the table's `SG` row names it `SIGINT`, not `KeyboardInterrupt` (the traceback is in stderr either way). Accepted:
   it is not an error a student writes on purpose; `HINTS` can carry `SIGINT` if wanted.
3. **Output is lost on a signal death.** `print('hi')` followed by SIGSEGV shows empty stdout in Python, and so does
   `printf("hello\n")` then a NULL write in C (stdout is a file, so fully buffered; abort/segv do not flush). This
   confirms the "Buffered stdout in C" open question and shows it applies to **Python too** whenever it dies by a
   signal (not for an exception, which flushes). Stdout is empty; stderr only has what was unbuffered (C's `assert`
   message does arrive).

Also: **`1/z` is not a reliable SIGFPE at `-O2`.** With `volatile int z = 0`, `printf("%d", 1/z)` printed `0` and
exited 0 (gcc rewrites `1/z` into a comparison); `1 % z` (and `a / b` with a non-constant `a`) dies `SG` 8. Step 7's
`error: SIGFPE` test must use something like `a / b` or `%` with both operands read from input or `volatile`, not
`1/0`. (A literal `1/0` is folded at compile time with a `-Wdiv-by-zero` warning; not tried.)

**`-lm`**: compile of a one-function program, 8 runs each: median 0.136 s without, 0.152 s with `-lm` (min 0.128 vs
0.140), so about 10-15 ms, noise level. Without `-lm`, `sqrt(non-constant)` **fails to link**
(`undefined reference to 'sqrt'`), so the flag is needed. `-Wall` was not measured separately (same order).

**`error_name`.** The regex in the design, `(?m)^([A-Za-z_][\w.]*)(?::|$)`, last match wins, is right for a plain
raise (`ValueError`), both chained forms (`ValueError`, the later traceback wins), `SyntaxError`, `IndentationError`,
`TabError`, `ModuleNotFoundError`, `AssertionError`, `RecursionError`, a `__main__` class (`MyErr`, with or without a
message), a dotted one (`json.decoder.JSONDecodeError`), an exception with a note. It **gives wrong names** for:

- a multi-line message: `raise ValueError('line one\nNotAnError: fake\nlast')` yields `last` (and a message line
  `Foo: bar` yields `Foo`);
- `sys.exit('bye')` (message printed to stderr, exit 1) yields `bye`; any program that prints a column-0 word to
  stderr and exits non-zero yields that word (the forgeable case the design already accepts, but here it is also
  accidental);
- an `ExceptionGroup` yields `None` (the name line is inside the `+ | ` box), which is merely a missing name.

**Adjusted regex** (anchor on the traceback's own shape: the name line directly follows an *indented* line, which
every frame's source line, caret line or `File` line is, and which a plain stderr line and the first line of a message
are not):

```
(?m)^[ \t]+.*\n([A-Za-z_][\w.]*)(?::|$)
```

Last match wins, group 1. Verified against every case above: the right names for all the good ones; `None` for
`sys.exit('bye')`, for a forged `Fake: forged` with no frame above it, for `warnings.warn` + `sys.exit(2)`, for
`sys.exit(1)` after printing a word, and for `ExceptionGroup`/`BaseExceptionGroup` (names `ExceptionGroup` are not
recovered; accepted, `error = None`); `ValueError` (not `last`) for the multi-line message. Still wrong only when a
message itself has an indented line followed by a column-0 word (`'a\n  indented\nlast'` gives `last`): the
forgeable case. It is also linear: 60 000 indented lines with and without a match take about 10 ms (the first
attempt, `^(?:[ \t]+.*\n)+(...)`, is quadratic on such input and hung; do not use it). Use this as the `[python]`
`error_name`.

## Steps

Each step leaves the suite green. The integration ones need isolate and nix.

1. **Spike, the run side only.** The compile side is answered by the compile plan. What remains: how isolate reports a
   signal death and `SIGXFSZ` in `--meta` for a C binary and for `python` (the `exitsig` field); that
   `EXERCISE_FSIZE_KB` at 256 KB truncates stdout and stderr as expected; what `-lm` costs; and the Python
   `error_name` regex against real tracebacks (a plain raise, a chained one, `SyntaxError`, a custom class in
   `__main__`, `KeyboardInterrupt`, an `ExceptionGroup`). A throwaway test; record the answers here before step 4.
2. **`cocco/languages.py`.** Move `load_languages` / `LANGUAGES_PATH` out of `judge.py`; add the optional keys
   `env`, `error_name`, `editor_mode` (and fill them for `python` and `c`, plus `editor_mode` for `cpp` and `java`);
   `-Wall` and `-lm` on `[c]`. `Language.run` takes `fsize_kb` and applies `env`. Existing challenge tests pass
   untouched; check that no challenge test or page assumes a successful C compile has empty stderr.
3. **`RunOutcome.exitsig`.** Read from the meta file; optional with a default. Unit-tested on synthetic meta.
4. **Schema.** `ExerciseExecution.error` replacing `exception`, with the before-validator for the old shape;
   `ExerciseCompile` and the optional `compile` fields; `HarnessReport` / `HarnessException` gone. Tests that a check
   and a run result stored before this (with `exception`, without `compile`) validate through `EXERCISE_JOB_RESULT`
   and come out with `error` and the traceback in `stderr`.
5. **The judge, Python only: drop the harness.** `exercise_language`, `prepare_exercise_program`,
   `run_exercise_program`, `read_exercise_run` (unit-tested on synthetic `RunOutcome`s for every row of the table);
   `check_exercise`, `run_exercise_job`, `build_exercise`, `build_version` on them; `harness.py`, `HARNESS_PATH`,
   `run_in_harness`, `read_execution`, `parse_harness_report`, `EXERCISE_ENV` deleted. `test_exercise_judge.py` changes
   where it tested harness semantics: prompts now fail a check, `error: LookupError` no longer passes on
   `IndexError`, `os._exit(0)` is a normal exit; everything else must pass as it is. `test_exercise_harness.py` (if
   it exists) goes.
6. **`format.py`.** `language:` checked against `languages.toml`, prefixes from the entry, a wrong-language prefix is
   an error, `error` is one word, `linguaggio` names `language`. Tests in `test_exercise_format.py`; the module
   docstring says the language key, the prefixes and "no prompts".
7. **C end to end.** Nothing language-specific to write, which is the point of this step; it is tests.
   Integration: hello world passes; a compile error is a check with `tests=[]` and diagnostics naming `main.c:LINE`;
   `error: SIGFPE` passes on `1/0`; a failed `assert` is `SIGABRT` with the assertion in stderr; an infinite loop is
   `timeout`; a flood is `truncated`; `return 3` is a failure with exit 3; `#include` of a host file the judge can read
   does not compile. Build: a solution that does not compile is an error with detail, a starter that does not compile
   is a warning, an `error: ValueError` in a C file fails the build naming the actual signal.
8. **Web: redaction, hints, session switch guard.** `_shown_error_name`; `error_hint` for each signal and without
   `line`; switching a session across languages is refused, as the swap target's fragment with an alert.
9. **Front end and seed.** Vendor `clike.js`, `data-mode`, `highlight(language)`, the compile block, "error output",
   stdin on Run, the prompt sentence, the badge. Route tests that a C exercise page loads `clike.js` and carries
   `data-mode="text/x-csrc"`, that a failed compile renders its diagnostics, that a stored pre-change Python result
   renders. A small C set in the dummy data (one exercise per verdict worth showing); remove prompts from the Python
   seed sets.
10. **Docs.** `.claude/rules/exercises.md` (no harness; the two calls; the error name; `languages.toml` keys; no
    prompts; what a new language needs), the `ExerciseDocument.language` comment, `DESIGN.md` if it says exercises are
    Python only or describes the harness, and a line in `judge.md` on the new `languages.toml` keys.

## Adding a language afterwards

For the record, and as the test of this design: `cpp` in exercises is `editor_mode` on its entry (already filled in
step 2), optionally an `error_name`, and nothing else. An interpreted language (say `[ruby]`) is a runtime in the
flake, an entry with `interpreter`, `extensions`, `editor_mode`, `error_name`, a vendored mode file, and the `HINTS`
lines for its common errors. Java additionally needs the `Main` source stem.

## Open questions

- **Buffered stdout in C.** A C program's stdout in the box is a file, so fully buffered: a program that prints three
  lines and then segfaults shows nothing. The earlier draft linked a `harness.c` constructor setting line buffering;
  that is a per-language harness again, so it is out. Python and Java flush on an uncaught error, so this is
  C/C++-only. If it bites in class, the least special fix is a compiler flag on the entry (`-include` of a bundled
  header), or sanitizers (next point).
- **Sanitizers.** `-fsanitize=address,undefined` would turn `SIGSEGV` into "heap-buffer-overflow at main.c:12" and
  flush on the way out. It costs memory and roughly 2× time and changes what an error name is. A follow-up plan.

## Outcome (2026-10-23)

Landed as planned, one implementer per step with a review after each; **not committed** (the whole change is staged).
`uv run pytest` is 1050 passed, integration included. Deviations from the design are all spike findings or review
fixes, listed per step.

1. **Spike**: results above. Three corrections carried into later steps: `RunOutcome` needs `exitcode` as well as
   `exitsig` (`retcode` is isolate's own); Python ignores `SIGXFSZ` (a flood is `RE`, exit 120, `OSError`), so the
   "stream at the cap" test comes before `RE`; the `error_name` regex became `(?m)^[ \t]+.*\n([A-Za-z_][\w.]*)(?::|$)`
   (the name line must follow an indented traceback line: a multi-line message or `sys.exit('bye')` no longer names an
   error). Also: `KeyboardInterrupt` is `SIGINT` (accepted); stdout printed before a signal death is lost in Python too.
2. **`cocco/languages.py`**: `LanguageSpec` (data only, with `env`, `error_name`, `editor_mode`, type-checked on load)
   and `load_languages(path, cls=)`; `judge.Language(LanguageSpec)` keeps `compile` / `run`, and `judge.load_languages`
   builds it. `run(fsize_kb=)` and the entry's `env` in both run branches. `[c]` gets `-Wall` and `-lm`. No challenge
   path shows a successful compile's output. `cocco/test_languages.py`.
3. **`RunOutcome.exitcode` / `exitsig`**: a pure `_parse_meta` / `_meta_int`; unit tests on synthetic meta, one
   integration test.
4. **Schema**: `ExerciseExecution.error`, `_fold_old_exception` (before-validator: `exception.type` → `error`,
   traceback appended to `stderr`), `ExerciseCompile`, optional `compile` on check and run results. The harness models
   lived in `judge.py` for one step to keep the suite green.
5. **Judge**: `exercise_language`, `prepare_exercise_program`, `run_exercise_program`, `read_exercise_run`;
   `check_exercise` / `run_exercise_job` / `build_exercise` on them (solution and starter each in their own
   subdirectory). `harness.py`, `test_exercise_harness.py`, `HARNESS_PATH`, `run_in_harness`, `read_execution`,
   `parse_harness_report`, `EXERCISE_ENV`, `_MAIN_IN_BOX`, `_HARNESS_IN_BOX` deleted; `EXERCISE_FSIZE_KB` = 256. An
   empty `error` block with a solution failing *without* a name (`return 3`) is a build error, as before; accepting it
   would need `grade` and the schema to represent "any failure". A program writing exactly 256 KB is `truncated`
   (accepted in the spike). Review: `_compile_detail` (the head of the diagnostics) instead of two `type: ignore`s.
6. **`format.py`**: `language:` (unknown is an error listing the known ones), prefixes from the entry, `<other
   language> <role>` is an error naming the right spelling while a bare ```` ```c ```` stays statement text, `error`
   is one word, `linguaggio`. `parse(languages=)` for tests.
7. **C end to end**: `cocco/test_exercise_c.py`, 16 tests, every item of the step; no production change was needed.
   `SIGFPE` uses `a / b` read from stdin (gcc folds `1/z` at `-O2`).
8. **Web**: signal hints, `error_hint(name)` without `line`, `_shown_error_name` (a `HINTS` key or `"error"`). The
   switch guard is a sequential check under the session's `FOR UPDATE` lock, like the others (they were never one
   statement); `_version_switch` shows the same refusal instead of the Switch button.
9. **Front end and seed**: `clike.js` 5.65.16 (byte-identical to unpkg's), `data-mode`, `editor_mode()` in
   `web/core.py`, `highlight(language)`, the compile block (failure as an alert, warnings collapsed), "Error output",
   "Input given" on Run, the prompt sentence, language badges (the set page's only once the version is ready). A
   three-exercise C set in the dev seed (pass / `SIGFPE` / `SIGABRT`); the seed had no Python sets, so no prompts to
   remove. `cocco/test_exercise_front_end.py`, 13 tests. Review: the redacted row carries `error`, not
   `exception.type`.
10. **Docs**: `.claude/rules/exercises.md` rewritten for execution, `judge.md` (keys, `exitcode`/`exitsig`,
    `languages.py`), `DESIGN.md`, `CLAUDE.md`'s guide line, stale harness comments in `schema.py`, `model.py`,
    `hints.py`.

Left open:
- **`npm run build:css` was not run** (no `node_modules` here). Step 9 uses literal classes only, but rebuild before
  shipping in case one (e.g. `ml-1`) is new to the sources.
- The open questions above (buffered stdout in C, sanitizers) stand; the spike showed the first applies to any
  signal death, Python's included.
- Sets already built by the harness: none in the repo; check deployed sets for `input("…")` prompts and base-class
  `error` blocks before switching sessions to a rebuilt version.
