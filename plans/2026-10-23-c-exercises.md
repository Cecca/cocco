---
status: pending
---

# C in the in-classroom exercises

## Goal

A teacher can write an exercise set in C, run it live in class exactly as a Python set is run today, and students
get the same Run / Check loop, with compiler diagnostics where Python would show a `SyntaxError`. Everything above
the execution layer stays as it is: sets, versions, sessions, pacing, progress, the dashboard, the projector,
transfer. A C set is the same `.md` file with one more front-matter line.

Python sets must not change. Every stored document, job result and Python harness run reads and behaves as it
does today.

## Where things stand (as of `928eba3`)

The design already left room for a second language, and it is almost the only seam:

- `schema.ExerciseDocument.language` exists, defaults to `"python"`, and its comment says a second language is "a
  new value rather than a new format". `query_job_exercise` already reads `document->>'language'` and hands it to
  the judge.
- `judge._python(language_name)` resolves it and refuses anything that is not interpreted. It is called from
  `run_exercise_job` and `build_version`.
- `languages.toml` already has `[c]` (runtime `gcc`), so the runtime is built and prewarmed at judge startup.

Python is hardwired everywhere else:

| Where | What assumes Python |
|---|---|
| `exercises/format.py` | front matter is `title` / `description` only; fence info accepts a `python` / `py` prefix; an `error` block must be a Python identifier, and the docstring says "the exception" |
| `exercises/harness.py` | the whole execution model: `input()` prompts, exceptions with an MRO, a line number, a traceback. It is a Python program run by the Python interpreter |
| `judge.run_in_harness` | runs `python harness.py <mode> main.py`; `EXERCISE_ENV` is `PYTHONHASHSEED`; `_MAIN_IN_BOX = "main.py"`; `build_exercise` writes `solution.py` |
| `judge.read_execution` | a run with status `OK` and no report is `no_report`; everything else comes from the report |
| `exercises/hints.py` | keyed on Python exception names |
| `web/exercise_sets._builtin_exception_name` | redacts a hidden test's exception to the nearest *Python builtin* class |
| `static/exercise.js` | CodeMirror `mode: "python"` |
| `_exercise-editor-head.jinja` | loads only `codemirror/python.js` |
| `exercise.jinja`, `f-exercise-drill-in.jinja`, `exercise-projector.jinja` | `highlight("python")` |
| `f-exercise-job.jinja` | "traceback" wording, `error_hint(type, line)` |

And one thing that matters more for C than it did for challenges: a compiler opens whatever the source names, so
compiling on the judge host leaks files (`#include "/etc/passwd"` quotes them in the diagnostics). For an exercise
that is every Check of every student in a room, on code typed a minute ago, with the diagnostics going straight
back to the student. **That hole is closed for challenges by `2026-10-23-compile-inside-isolate.md`, which has
landed** (`5e377ed`, `3aaef38`): `isolate_run` can collect files back out of a box and `Language.compile` runs in
one. This plan does not introduce that mechanism; it uses it. Exercises must not compile on the host, and `CRunner`
compiles through `isolate_run` as below.

## Design decisions

### The language is a front-matter key: `language: c`

```markdown
---
title: Pointers
language: c
---
```

`FRONT_MATTER_KEYS` gains `language`. Absent means `python`, so every existing file means what it meant. The
value is checked against `EXERCISE_LANGUAGES = ("python", "c")` in `format.py`, and anything else is an error
that lists the two, at import, like every other parse problem. Not a warning: a C file read as Python would build
every solution into a `SyntaxError`, and a build is the slow, confusing place to find that out.

The fence prefix follows the language: `c starter` / `c solution` in a C file, `python starter` / `py starter` in a
Python one, as today. A `python solution` block in a C file is an error naming `c solution`, for the reason
`_ITALIAN` exists: left alone it would be "any other code block", which is part of the statement, solution and all.
`linguaggio` joins `_ITALIAN`.

The compiled document keeps `format: 1`. `language` was always in it; only its set of values grows.

### One "runner" per language in the judge, behind one interface

`_python` becomes `exercise_runner(language_name) -> ExerciseRunner`, with two implementations. The interface is
what `check_exercise`, `run_exercise_job` and `build_exercise` actually need:

```python
class ExerciseRunner(Protocol):
    source_name: str                     # "main.py" / "main.c"
    async def prepare(self, source: str, workdir: Path) -> Prepared | CompileFailure: ...
    async def run(self, prepared: Prepared, mode: str, stdin: str,
                  time_limit: float, workdir: Path) -> ExerciseExecution: ...
```

- `PythonRunner.prepare` writes `main.py` and never fails (a `SyntaxError` stays a runtime exception reported by
  the harness, exactly as today). `PythonRunner.run` is today's `run_in_harness` + `read_execution`, moved, not
  rewritten.
- `CRunner.prepare` compiles once, in a box (below). `CRunner.run` runs the binary in a box and builds the
  `ExerciseExecution` from isolate's outcome (below).

`check_exercise` prepares once and runs every test on the result, so a Check costs one compile plus N runs, never
N compiles. `build_exercise` prepares the solution once (used for both of its rounds) and the starter once. The
hardcoded `solution.py` goes: each preparation gets its own subdirectory of the job's `workdir`.

`Language` and the challenge path are not touched. The C runner reads `runtime` and `compiler` from the `[c]` entry
of `languages.toml` but carries its own flags (below), because an exercise wants warnings and a challenge does not.

### C compiles inside an isolate box

`CRunner.prepare` calls `isolate_run` (the primitive that landed with the compile-in-isolate plan; read its
docstring and "Spike findings (step 1)" there rather than anything restated here) with the gcc runtime's `gcc`
(`resolve_runtime`) as the executable, `main.c` and `harness.c` (below) as `box_files`, the flags as arguments, and
`collect=["main"]`, `collect_into=<the preparation's subdirectory of the job's workdir>`. The box is gcc's working
directory, so diagnostics say `main.c:LINE`, the name the student knows, and `#include "/etc/passwd"` is "no such
file".

What landed, and where it differs from what this plan first assumed:

- The parameter is `collect: List[str] | None` plus a required-with-it `collect_into: Path`, and the entries are
  **top-level glob patterns**, not bare names (`"main"` is a pattern that happens to match one file). A `/` or `..`
  in one is a `ValueError`.
- Only regular files come out and none is followed: a symlink named `main` is skipped and logged, not copied. A
  program that leaves `main` as a symlink therefore looks the same as a failed link: absent.
- Collection is over `fsize_kb`: more collected bytes than that is a **`RuntimeError`** (a judge error), not a
  truncation. With `COMPILE_FSIZE_KB` the binary cannot reach it on its own, but `prepare` must not turn that
  `RuntimeError` into a `CompileFailure`.
- Absent is not an error: `prepare` sees a failed compile as `main` not being in `collect_into`, decided together
  with the status.
- `RunOutcome` has **no `exitsig` yet**; step 2 below adds it for the run side.

**Limits and outcome mapping are the challenge compile's, reused.** The compile budget is `COMPILE_TIME_LIMIT_S`
(10 s CPU), `COMPILE_MEM_LIMIT_KB` (1 GB), `COMPILE_PROCESSES` (16) and `COMPILE_FSIZE_KB` (64 MB), with
`COMPILE_ENV` (empty) as the environment; there are no `EXERCISE_COMPILE_*` constants. The compile plan's spike
found gcc needs 4 processes and a few hundred MB at most, so this plan's earlier smaller numbers (8 processes,
512 MB) would be a tuning for a cost that is not there, and a second set of constants is a second place for the two
paths to disagree; an exercise compile is no cheaper to sandbox than a challenge one. Revisit only on a concrete
reason (none is known): if exercises ever need a shorter budget, say so here with the reason, as a constant defined
beside `COMPILE_*`. The nix gcc needs no environment and no `-pipe` (spike). The outcome order is the compile
plan's: `TO`, then `oom` (before the status, since gcc reports an OOM-killed `cc1` as `RE`), then `RE`, then
`SG` / `XX`, then a missing artifact; each of those is a `CompileFailure` whose message is the compiler's stderr
plus the reason, the same wording as `Language.compile` (`COMPILE_TIME_LIMIT_S` etc. are read at call time, so tests
patch them). `XX` is also logged at `error`. A student can write a header that takes the compiler forever; that is a
`CompileFailure`, not a judge error. Whether to call `Language.compile` itself rather than `isolate_run` is for
step 6 to decide: it already does all of the above for `c`, but its flags are the challenge's, and `CRunner` wants
its own and two sources, so expect to call `isolate_run` and reuse only the mapping (factor it out of
`Language.compile` if copying it would be the alternative).

Flags: `-std=c17 -Og -Wall -Wextra -fdiagnostics-color=never -o main main.c harness.c -lm`. `-Og` rather than `-O2` so that undefined behaviour in a beginner's loop is less likely to be
"optimized" into something nobody can explain on a projector, while keeping the warnings that need optimization
(`-Wmaybe-uninitialized`). `-lm` because `math.h` is the first thing a C exercise reaches for. Caret display stays
on (it helps beginners); the open question is whether to keep it.

### The C "harness" is a constructor and the judge's reading of isolate's outcome

There is no interpreter to wrap, and the two Python semantics the harness exists for do not carry over:

- **Prompts are output.** C cannot tell a prompt from a result. A C exercise that wants a prompt spells it out in
  the expected output; the format docstring and the guide say so. In `run` mode there is no echo of the line read
  (that would need ptrace), so the Run tab shows the stdin given above the transcript for a C set.
- **There are no exceptions.** What C has is death by signal. The runner reports a signal as a
  `HarnessException` with `type` = the signal's name (`SIGSEGV`), `classes = [type]`, `line = None` and
  `traceback` = the tail of stderr (where `assert` writes `main.c:7: main: Assertion 'n > 0' failed.`). Then
  `grade`, `error_matches` and every template keep working unchanged.

`cocco/exercises/harness.c` is a few lines linked into every program: an `__attribute__((constructor))` that sets
stdout to line buffering. Without it, stdout is a file inside the box, fully buffered, and a program that prints
three lines and then segfaults shows *nothing*, which is the single most baffling thing a beginner could meet here.
Line buffering is also what a terminal at home does. It is the C side's whole harness, and like `harness.py` it is
not imported by cocco; `HARNESS_C_PATH` sits beside `HARNESS_PATH`.

`read_native_execution(res)`, beside `read_execution`:

| isolate | `ExerciseExecution` |
|---|---|
| `TO` | `timeout` |
| `oom` | `memory` |
| `XX` | raises (judge error, as today) |
| `OK` | `ok`, `exit = 0` |
| `RE` | `ok`, `exit = retcode`, no exception: `return 1` is Python's `sys.exit(1)` |
| `SG`, `SIGXFSZ` | `ok`, `truncated = True`: the program printed past `fsize` |
| `SG`, any other | `ok`, `exception` = the signal |

There is no `no_report` for C: there is no report to forge or lose. `RunOutcome` gains `exitsig: int | None`,
read from isolate's meta file, since `message` ("Caught fatal signal 11") is prose. stdout and stderr are cut at
the harness's `LIMIT` (256 K characters) with `truncated` set past it, then go through `_jsonb_safe`, the same
boundary Python's report goes through, so what is compared and what is stored stay the same bytes.

`EXERCISE_ENV` stays Python's; the C runner passes none.

### `error:` blocks name a signal in C

```markdown
```error
SIGFPE
```
```

`format.py` validates the name per language: an identifier in Python, one of `C_SIGNALS` (`SIGSEGV`, `SIGFPE`,
`SIGABRT`, `SIGBUS`, `SIGILL`) in C, with an error that lists them. An empty block still means "whatever the
solution ends with". The build's message "add an error block with X" works unchanged, X being `SIGABRT`.

### Compile errors are a result of their own, not a verdict per test

A check that does not compile has run nothing, so N identical rows saying "compile error" would be noise. Both
result shapes gain an optional field:

```python
class ExerciseCompile(Base):
    ok: bool
    diagnostics: str     # gcc's stderr, capped at MAX_EXERCISE_STORED_OUTPUT

class ExerciseCheckResult(Base):
    ...
    compile: ExerciseCompile | None = None   # None for Python, and for every result stored before this

class ExerciseRunResult(...):
    compile: ExerciseCompile | None = None
```

A failed compile is `passed=False, tests=[]` for a check, and an empty execution for a run, with `compile.ok =
False`. Optional with a default, so every stored result still validates through `EXERCISE_JOB_RESULT`: **no
migration**. A successful compile with warnings keeps them, and the page shows them above the tests, collapsed.

Progress needs nothing: a check that did not compile has `passed = false` and so makes the exercise *attempted*,
which is what `_exercise_status` already decides.

Diagnostics are about the student's own source and nothing else (the compile has no stdin and no hidden test), so
they are shown in full even where hidden tests are redacted.

In a build: a solution that does not compile is an **error** with the diagnostics as `detail`. A starter that does
not compile is a **warning** ("students start from code that does not compile"), since a starter with a gap to fill
may be deliberate, but the teacher should know.

### Hidden tests: redaction knows the language

`_builtin_exception_name` is Python's rule. For C the program chooses its signal too (`raise(SIGUSR1)`), so the
same bound applies: the name shown is one of `C_SIGNALS`, else `"signal"`. `exercise_check_rows` takes the
language from the version it already receives. The leak stays what that docstring accepts: a few bits per hidden
test per Check.

`hints.py` gains entries for the C signals (`SIGSEGV`: "invalid memory access: an index past the end of an array,
or a NULL or uninitialised pointer", `SIGFPE`: "integer division by zero", `SIGABRT`: "an assert failed, or abort()
was called", …). The names cannot collide with Python's, so one `HINTS` dict stays; `error_hint` is unchanged.

### A session's language is fixed

Switching a session to a newer version is refused when the new version's language differs from the pinned one's,
in the same conditional statement as the other switch guards, with a message saying so. Every student's draft in
the browser, every past check and every "solved" was C or Python; mixing them on one session has no meaning anyone
asked for. A *set* may change language between imports (it is only content), and a new session picks it up.

### Front end

- Vendor CodeMirror 5's `clike.js`, the same release as the `codemirror.js` already in `static/codemirror/`, MIT
  like the rest. `_exercise-editor-head.jinja` loads both mode files: a few KB, and no per-page branching.
- The editor's textarea carries `data-language`, and `exercise.js` maps it: `python` → `"python"`, `c` →
  `"text/x-csrc"`. Tab still inserts spaces; the comment that justifies it is Python's, and becomes "spaces, never
  a tab character: Python minds, and C reads the same either way".
- `highlight("python")` → `highlight(language)` in the three templates. `render_source` already takes any Pygments
  alias, and `c` is one.
- `f-exercise-job.jinja`: a compile block (diagnostics in the same `<pre>` style as the traceback), "traceback"
  becomes "error output" when the language is C, and the Run tab shows the stdin for C.
- The set page and the session card show the language as a small badge, so a teacher opening a session can tell.

### What does not change

Transfer (`cocco pull --set` / `push`): the file carries `language:`. The CLI, the `ETag` rule and the push lock
are untouched. Sessions, pacing, reveals, the dashboard, the projector, deletion, retention: untouched. The
challenge judge path, `Language`, `languages.toml`: untouched.

## Steps

Each step leaves the suite green. The integration ones need isolate and nix.

1. **Spike, reduced to what is exercise-specific.** The environment, process and memory questions for gcc in a box
   are answered in the compile plan's "Spike findings (step 1)" (empty environment, 4 processes, `/tmp` fine). What
   remains is the run side: how isolate reports a signal death and `SIGXFSZ` in `--meta` for a C binary (the
   `exitsig` field; the compile spike saw `status:SG`, `exitsig:9` for an OOM-killed javac), whether `-Og` with
   `-Wall -Wextra` and `harness.c` links under the real flags, and what `-lm` costs. A throwaway test; record the
   answers in this plan before step 3.
2. **`RunOutcome.exitsig`.** `isolate_run(collect=…)` already exists; this step only reads `exitsig` from the meta
   file into `RunOutcome` (an optional field with a default, so the existing constructions are untouched).
   Unit-tested on synthetic meta.
3. **`format.py`: `language:`, the C fence prefix, signal names in `error`.** Tests in `test_exercise_format.py`:
   default is python, unknown language is an error listing both, `python solution` in a C file is an error, a bad
   signal name is an error, `linguaggio` names `language`. Update the module docstring.
4. **Schema: `ExerciseCompile`, the optional `compile` fields.** A test that a result stored before this (no
   `compile` key) still validates through `EXERCISE_JOB_RESULT`.
5. **Runner interface, Python only.** Move `run_in_harness` / `read_execution` behind `PythonRunner`; switch
   `check_exercise`, `run_exercise_job`, `build_exercise`, `build_version` to it. A pure refactor: the existing
   `test_exercise_judge.py` must pass untouched.
6. **`CRunner` and `harness.c`.** Compile in a box (`isolate_run(collect=["main"], …)` with the `COMPILE_*` budget), run, `read_native_execution`. Unit tests on synthetic
   `RunOutcome`s for every row of the table. Integration tests: hello world passes; a compile error is a check with
   `tests=[]` and diagnostics naming `main.c:LINE`; warnings survive a successful compile; segfault after printing
   keeps the printed lines (the constructor); `error: SIGFPE` passes on `1/0`; a failed `assert` is `SIGABRT` with
   the assertion in the traceback; an infinite loop is `timeout`; a flood is `truncated`; `return 3` is exit 3;
   **`#include` of a host file the judge can read does not compile**.
7. **C in the build.** Solution not compiling is an error with detail; starter not compiling is a warning; the
   non-determinism round catches a solution printing an uninitialised variable (where gcc lets it).
8. **Web: redaction, hints, session switch guard.** `exercise_check_rows` with a C version; `error_hint` for each
   signal; switching a session across languages is refused, and the refusal is the swap target's fragment with an
   alert.
9. **Front end.** Vendor `clike.js`, `data-language`, `highlight(language)`, the compile block, the C wording, the
   badge. Route tests that a C exercise page loads `clike.js` and carries `data-language="c"`; that a C check
   result with a failed compile renders the diagnostics; that a Python page renders as before.
10. **Dev seed and docs.** A small C set in the dummy data (one exercise per verdict worth showing). Update
    `.claude/rules/exercises.md` (the language key, the runner, compile in a box, signals, prompts), the
    `harness.py` docstring (it is Python's harness now, not *the* harness), the `ExerciseDocument.language` comment,
    and `DESIGN.md` if it says exercises are Python only.

## Open questions

- **Flags.** `-Og` vs `-O0`; `-Wall -Wextra` vs adding `-Wpedantic`; whether to keep gcc's caret display. Teachers
  may want a `cflags:` front-matter key eventually; not in this plan, since every flag is an input to the compiler
  on code from a whole classroom.
- **Sanitizers.** `-fsanitize=address,undefined` would turn "SIGSEGV" into "heap-buffer-overflow at main.c:12",
  which is the line number Python students get and C students won't. It costs memory (ASan's shadow) and roughly 2×
  time, needs the memory limit revisited, and changes what a "signal" is. Worth a follow-up plan once the base works.
- **C++.** With the runner interface, `cpp` is a third value sharing `CRunner` with different flags and a different
  source name. Deliberately left out so this plan stays one language.
- **Compile on the host for challenges.** Settled elsewhere: `2026-10-23-compile-inside-isolate.md` has landed.
