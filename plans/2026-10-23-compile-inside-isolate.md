---
status: pending
---

# Compile inside isolate

## The problem

`Language.compile` runs the compiler with `asyncio.create_subprocess_exec` **on the judge host**, as the judge's
user, in the run's temp dir, with the judge's environment and no limits. Only the *run* goes through `isolate_run`.
`DESIGN.md` calls compiling "a trusted operation on untrusted source"; it is not. A compiler reads whatever files
the source tells it to, and it does so before any sandbox exists.

What a student (or course staff, through an evaluator: the previous plan already settles that staff are not trusted
with the judge host) can do today:

- **Read a file through the diagnostics.** `#include "/etc/passwd"` fails to compile with an error that quotes the
  file's lines, and compiler output is shown on the submission page **for every test case, hidden ones included**
  (`domain-rules.md`: "compiler output is exempt" from `visible_outcomes`). Any file the judge user can read is in
  reach: `/proc/self/environ` (the judge's environment, so the database URL and any secret the process was given),
  `[judge] gcroots`, the config file, other courses' hidden expected outputs under `[storage] root` (gzip, so
  garbled, but bytes leak).
- **Read a whole file through the binary.** `__asm__(".incbin \"/etc/shadow-readable-thing\"")` (any gcc; the
  assembler opens the path) or `#embed` (gcc ≥ 15, accepted as an extension under `-std=c17`) puts the file's bytes
  *into the executable*, and the program then prints them on a sample test case. No error, no size limit, binary-safe:
  a gzip file from storage comes out intact and can be inflated at home.
- **Read through the working directory.** javac's default class path and source path are `.`, the judge's working
  directory, and a relative `#include "cocco.toml"` resolves against the source's directory then the include path.
  The previous plan (`2026-10-17-judge-guessing-of-filenames.md`, option C) makes the cwd empty; this plan makes it
  irrelevant to the compiler too.
- **Starve the host.** No CPU, wall, memory, process or file-size limit: a template or `constexpr` bomb, a
  `#include "/dev/zero"` or a header that includes itself through a macro trick holds a worker indefinitely, can eat
  the judge's memory, and fills `/tmp` with object files. With `[judge] concurrency` workers, a handful of uploads
  stops grading for everyone.

Python is not affected: it has no compile step, and its syntax errors already happen inside the box.

The C exercises plan (`2026-10-23-c-exercises.md`) reached the same conclusion for exercises and designs its own
in-box compile, with an `isolate_run(collect=...)` parameter. **This plan should land first and provide that
mechanism**, so exercises reuse it rather than introduce it.

## Design

### Compile with `isolate_run`, get the artifacts back with `collect`

`Language.compile(source, workdir)` keeps its signature and its `CompileOutcome`, so `run_submission`,
`prepare_evaluator` and the tests do not change shape. Inside, it calls

```python
isolate_run(
    compiler,                      # resolve_runtime(...), an absolute /nix/store path, as for interpreters
    None,                          # empty stdin
    fmt_args,                      # {source}, {output} as basenames, {workdir} as "."
    time_limit=COMPILE_TIME_LIMIT_S,
    mem_limit_kb=COMPILE_MEM_LIMIT_KB,
    processes=COMPILE_PROCESSES + self.additional_processes,
    fsize_kb=COMPILE_FSIZE_KB,
    env=COMPILE_ENV,
    box_files={source.name: source},
    collect=self._compile_artifacts(source),   # e.g. ["hello"] or ["*.class"]
    collect_into=workdir,
)
```

The placeholders become box-relative exactly as `Language.run` already formats `{executable}` and `{workdir}`.
The box is the compiler's working directory, so diagnostics say `hello.c:3:5:` instead of
`/tmp/tmpk2j…/hello.c:3:5:`, which also stops leaking the judge's temp path into every compile error.

The box sees `/nix/store` (already `--dir`) and isolate's own `/proc`, `/dev` and `/tmp`, and nothing else of the
host, so `#include "/etc/passwd"` is "No such file or directory" and `.incbin` fails to assemble. `/proc/self` is the
box's own process, whose environment is what `COMPILE_ENV` sets and nothing more.

### `isolate_run` gains `collect` / `collect_into`

Today a box's files die with `_cleanup_box`; only `_stdout` / `_stderr` come back, as strings. New parameters:

- `collect: List[str] | None`: **top-level** glob patterns in the box (`"main"`, `"*.class"`), matched after the
  run and before cleanup. Patterns, not just names, because javac writes one `.class` per class and only it knows
  which (`Main$Helper.class`), the same reason `_run_artifacts` globs. Still not guessing in the `c85a93d` sense:
  the caller names what it wants, and the match is over the box's own output, never a host path.
- `collect_into: Path`: where matches are copied, under their box name.

Rules that make it safe, each one a sentence in the docstring and a test:

- **Regular files only, never followed.** The program in the box controls the box directory. A symlink
  `hello -> /etc/shadow` there would be read by the *judge* on the way out, as the judge's user. Open each match
  with `O_NOFOLLOW | O_NONBLOCK` and check `S_ISREG` on the `fstat` of the open descriptor, rather than an
  `lstat` first and an `open` after: the box's processes are dead by then, but checking the descriptor you
  actually read leaves nothing to argue about (`O_NONBLOCK` so a FIFO cannot hang the open). Skip directories,
  FIFOs, devices and symlinks, and log them at `warning`.
- **Not `_stdout` / `_stderr`**, which are the judge's: refused even if a pattern matches them.
- **Bounded.** `fsize_kb` already caps every file the program writes; collect checks the total against it once more
  rather than trusting that it applied.
- **Absent is not an error.** A compiler that failed produces nothing, and `compile` decides from the status and
  from what came back.

`collect` is applied only on the normal path, never in the `except BaseException` cleanup.

### What `compile` reports

| Box result | `CompileOutcome` |
| --- | --- |
| `OK` and the expected artifact collected | success, `executable = workdir / output` |
| `OK` but nothing collected | failure, "the compiler produced no `<output>`" (a judge-side bug or a strange source; never silent) |
| `RE` (non-zero exit) | failure, the compiler's stdout/stderr, as today |
| `TO` | failure, "compilation took longer than 10 s" |
| OOM | failure, "compilation ran out of memory (limit 1024 MB)" |
| `SG`, `XX` | failure with isolate's message; `XX` is also logged at `error`, since it is the judge's problem |

A failed compile stays `compile_failure` for a submission and `EvaluatorError` for an evaluator. It is
deliberately **not** `timeout` / `out_of_memory`: those are verdicts on the student's *program*, and the
`running_time` leaderboard reads them. That is the same reasoning `_describe_evaluator_failure` gives for checkers.
`_classify_isolate_failure` is therefore not used here.

`CompileOutcome` keeps `stdout` / `stderr` as strings; isolate already reads them whole, and `fsize` bounds them.

### Budget: module constants, not the challenge's limits

Beside `EVALUATOR_TIME_LIMIT_S`, for the same reason it exists (a teacher must not raise the students' run limits
to make compilation fit, and a student must not get a bigger compile budget by asking for one):

- `COMPILE_TIME_LIMIT_S = 10.0` (CPU; the wall limit follows from `isolate_run`'s `2 * t + 1`)
- `COMPILE_MEM_LIMIT_KB = 1024 * 1024` (javac is a JVM; gcc at `-O2` on a big file needs a few hundred MB)
- `COMPILE_PROCESSES = 16` (gcc runs `cc1`/`cc1plus`, `as`, `collect2`, `ld`), plus the language's
  `additional_processes` so javac gets the JVM's threads, as `run` does
- `COMPILE_FSIZE_KB = 64 * 1024` (the binary and the diagnostics; also what stops `#include "/dev/zero"`
  from writing forever)

The exercise plan's `EXERCISE_COMPILE_*` should become these, or be defined in terms of them; an exercise compile
is not cheaper to sandbox than a challenge one. Settle that in that plan, not here.

### The nix wrappers need an environment

isolate empties the environment. The nix `gcc`/`g++` are wrapper scripts and javac is a launcher; what they need
(`PATH` for `as`/`ld`? `TMPDIR`? `HOME`? `NIX_*` hardening flags? JVM `-Xmx` vs. the cgroup limit?) is not known
yet. **Step 1 is a spike that settles it**, the same spike the C exercises plan has as its step 1; do it once, here.
Whatever is needed becomes `COMPILE_ENV`, a constant, never the judge's own environment passed through (that is
exactly the leak through `/proc/self/environ` this plan closes). `PATH`, if needed, is built from the resolved
runtime's store paths, not copied from the host.

If gcc needs a writable `/tmp`, isolate's default `/tmp` is a private, per-box directory and is fine; `-pipe` is
the fallback.

### The box pool and the cost

A compile now holds a box id from `_IDS_POOL` for up to ~21 s wall. With 32 boxes and `concurrency` ≤ 16 that is
still at most one box per worker at a time, since a worker compiles *or* runs. The overhead is one isolate
`--init` / `--cleanup` per submission (and one per evaluator), tens of milliseconds against a compile that takes
hundreds. Measure it in the spike, but it does not change the design.

## Spike findings (step 1)

Run by hand with `isolate --cg --box-id=90 …`, `--dir=/nix/store`, the source copied into the box, the box as cwd, the
Design's limits (`--time=10 --wall-time=21 --cg-mem=1048576`, `--fsize=65536` where noted). Runtimes from the prewarm
paths: gcc-wrapper-15.2.0 (so `#embed` exists), openjdk-21. isolate 2.5; the host is 16 threads. Scripts were throwaway.

**The design holds; the one open question resolves to "no environment needed".**

- **`COMPILE_ENV` is empty**, i.e. `env=None`, nothing passed. gcc, g++ and javac compile hello-world (javac with a
  `Main$Helper` inner class, both `.class` files appear) with isolate's own default environment, which is the single
  variable `LIBC_FATAL_STDERR_=1` (read back from `/proc/self/environ` in the box). The nix `gcc`/`g++` are bash scripts
  whose shebang and `as`/`ld`/`cc1` paths are absolute store paths, and they set their own PATH; javac finds its JVM by
  its own path. No `PATH`, `TMPDIR`, `HOME` or `NIX_*` is needed, so there is nothing to derive from the resolved
  runtime and nothing that could be copied from the host. Keep it that way: `COMPILE_ENV` should not be a constant to
  maintain, and `/proc/self/environ` then holds only isolate's own line. Drop the "`PATH` is built from the runtime's
  store paths" fallback from the Design unless a later nix bump needs it (the step 3 tests will say).
- **Process counts** (`--processes=N`, smallest that works): gcc 4 (3 fails in `ld`, 2 in `collect2`), g++ 4, javac
  about 32 (below 16 the VM does not start; 20 and 24 compile but print `Failed to create worker thread` GC warnings;
  32, 48, 64 are clean). The JVM's thread count follows the host's core count, so it is not a constant of the language.
  `COMPILE_PROCESSES = 16` plus java's `additional_processes = 64` (= 80) is ample for all three; 16 alone would
  **not** start javac, so the `+ self.additional_processes` in the Design is load-bearing.
- **javac memory**: starts and finishes at `--cg-mem=1048576` with no flag (peak cgroup memory about 60 MB for hello
  world; it still compiles at `--cg-mem=131072`, the JVM sizes its heap from the cgroup). **No `-J-Xmx…` and no
  `languages.toml` change.** A 940 KB generated `Big.java` (20 000 methods) ends as `TO` at 10 s with 215 MB, not OOM.
  At a 64 MB limit the OOM shows as `status:SG`, `exitsig:9`, `cg-oom-killed:1`.
- **`/tmp`**: gcc does use it (`as -o /tmp/cc….o /tmp/cc….s`). isolate's default per-box `/tmp` (a private, initially
  empty directory) is enough, with `TMPDIR` unset; a bogus `TMPDIR` falls back to `/tmp` anyway. `-pipe` is not needed.
  Diagnostics from the assembler name `/tmp/ccXXXX.s`, the box's own, which leaks nothing. `--fsize` bounds each file,
  not the directory.
- **The hole is closed**, inside the box: `#include "/etc/passwd"` → `fatal error: /etc/passwd: No such file or
  directory`; `#embed "/etc/passwd"` the same; `__asm__(".incbin \"/etc/passwd\"")` → `Error: file not found:
  /etc/passwd`; C++ `#include "/proc/self/environ"` compiles and contributes nothing of the judge's (the box process's
  own environ); javac with a `Secret.java` in the *host's* cwd → `cannot find symbol` (its cwd is `/box`). Caveat: what
  the box sees comes from **isolate's config** (here `/bin /dev /lib /lib64 /usr /proc /tmp /nix /box`, no `/etc`).
  A host whose isolate config also binds `/etc` would make `/etc/passwd` readable, which step 4's test (it compares
  against the host's real `/etc/passwd`) catches; worth a line in the deployment notes.
- **No network**: the box has its own network namespace with only `lo` (`/proc/net/dev`). A `connect()` to a listener
  on the host's `127.0.0.1` gets `Connection refused`, and to `8.8.8.8` `Network is unreachable`.
- **`/dev` is the host's `/dev`, and `/dev/zero` is there** (with `null`, `full`, `urandom`, `random`; `mem` is not
  readable by the box uid). But **`#include "/dev/zero"` is an OOM test, not a time-out test**: gcc reads it into
  memory until the cgroup limit, in 0.3 s, `status:RE`, `exitcode:1`, `cg-oom-killed:1`, stderr `gcc: fatal error:
  Killed signal terminated program cc1`. The same with `const char d[]={ #include "/dev/zero" };`. `--fsize` plays no
  part. Template bombs also end as OOM (T<100,100,100>) or hit gcc's depth limit (900). A single `constexpr` loop is
  stopped by gcc itself (`-fconstexpr-loop-limit` 262144, `-fconstexpr-ops-limit` 33554432, about 3.4 s per
  evaluation), so it ends as a plain compile error.
- **Recommendation for step 4's time-out test**: a C++ file with ten `constexpr long long vN = f(N);` using a nested
  loop (`for a < 4000, for b < 1500, s += (a ^ b ^ k) & 3`), each stopping at the ops limit after about 3.4 s; the
  whole compile ends as `status:TO`, `message:Time limit exceeded`, `time:10.07` in both runs, reliably. It costs the
  full 10 s, so the test should `monkeypatch` `COMPILE_TIME_LIMIT_S` down rather than wait (not measured at a lower limit: check that a single constant, 3.4 s, is then enough). Keep `#include "/dev/zero"` as the **OOM** test (`cg-oom-killed`, 0.3 s, cheap). javac's
  `Big.java` above is a Java time-out if one is wanted, but it is a 1 MB file.
- **`--meta`** (the fields `isolate_run` reads: `status` absent means OK, `exitcode`, `cg-oom-killed`):

  | Case | `status` | `exitcode` / `exitsig` | `cg-oom-killed` | `message` |
  | --- | --- | --- | --- | --- |
  | success | (absent) | `exitcode:0` | (absent) | (absent) |
  | compile error (gcc or javac) | `RE` | `exitcode:1` | (absent) | `Exited with error status 1` |
  | time-out | `TO` | (none) | (absent) | `Time limit exceeded` |
  | OOM, gcc | `RE` | `exitcode:1` | `1` | `Exited with error status 1` |
  | OOM, javac | `SG` | `exitsig:9` | `1` | `Caught fatal signal 9` |

  So an OOM is **not** a distinct status: it is `RE` when the driver (`gcc`) survives its killed `cc1` and reports it,
  `SG` when the top process is the victim. `compile` must test `oom` **before** it looks at `status`, which is what
  `_classify_isolate_failure` already does for runs. The failed-compile `status:RE` is also what a gcc OOM looks like,
  so the order matters: `TO`, then `oom`, then `RE`/`SG`.
- **Overhead**: one `--init` is 3 to 6 ms and one `--cleanup` 2 to 7 ms, about 10 ms per box. Compile wall time in the
  box against the host: C 70 to 90 ms against 90 to 100; C++ 250 ms against 270 to 280; javac 260 to 310 ms against
  270 to 290 (the very first javac after a build was 770 ms, a cold cache). No measurable cost of the box; the
  per-submission overhead is the pair above, once.

## Steps

1. **Spike** (throwaway script, not committed): run `gcc`, `g++` and `javac` from the prewarmed runtimes through
   `isolate --run` by hand with an empty environment, `--dir=/nix/store`, and the source in the box. Find the
   minimal `COMPILE_ENV`, the process count each actually uses, and javac's memory at the 1 GB cgroup limit (whether
   it needs `-J-Xmx…` in `languages.toml`). Confirm `#include "/etc/passwd"` and `.incbin "/etc/passwd"` fail and
   what isolate's `/dev` contains. Write the findings into this plan before step 2.
2. **`isolate_run(collect=, collect_into=)`** with the rules above, plus `_cleanup_box` unchanged. Tests
   (`integration`, they need isolate), driven by a small Python program as the box executable, like the existing
   `isolate_run` tests:
   - a named file and a glob are copied out, nothing else is;
   - a symlink named in `collect` pointing at a host file the judge can read is **not** collected, and the host
     file's content appears nowhere;
   - a directory, a FIFO, `_stdout` are not collected;
   - a pattern matching nothing collects nothing and raises nothing.
3. **`Language.compile` through the box**, with the constants, `COMPILE_ENV`, box-relative placeholders and the
   outcome table. `_compile_artifacts` derives the patterns the way `_run_artifacts` derives what to ship
   (`*<output_ext>` for a compiled + VM language, the `{output}` basename otherwise), and lives beside it. Update
   the `Language` docstring.
4. **Tests for the hole itself** (`integration`, in `test_judge.py`), each through `run_submission` so the
   assertion is about what a student would see on the page:
   - C `#include "/etc/passwd"` → `compile_failure`, and no outcome's `message` contains a line of the host's
     `/etc/passwd` (read it in the test and check for its first line, rather than assuming `root:`);
   - C `__asm__(".incbin \"/etc/passwd\"")` plus a `main` printing the symbol → `compile_failure`, or if it
     somehow compiles, stdout does not contain that line;
   - C++ `#include "/proc/self/environ"` with a marker variable set in the judge's environment by `monkeypatch` →
     the marker appears nowhere;
   - java with `monkeypatch.chdir` to a directory holding `Secret.java`, and a `Main` that uses `Secret` →
     `compile_failure` (javac's class path is the box, not the judge's cwd);
   - a compile that does not finish (C `#include "/dev/zero"` if the spike shows isolate's `/dev` has it, else a
     C++ `constexpr` loop past the time limit) → `compile_failure` with the time-out message, in well under the
     test timeout, and the box id is back in the pool afterwards;
   - an evaluator in C with `#include "/etc/passwd"` → `evaluator_error`, the same no-leak assertion
     (`MAX_EVALUATOR_COMPILE_OUTPUT` truncates but does not redact, so it is no defence on its own).
   Existing tests: `test_language_c/cpp/java`, `test_run_submission_java_keeps_its_class_name`, the compile-failure
   and evaluator-compile tests should pass unchanged; any that assert on an absolute path in a diagnostic change to
   the box-relative one.
5. **Docs.**
   - `DESIGN.md`, "For compiled languages": step 1 becomes "compile in its own isolate box", with one sentence on
     why it is not a trusted operation (`#include`, `.incbin`, `#embed`, javac's `.`).
   - `.claude/rules/judge.md`: layer 2 mentions `collect` and its symlink rule; layer 3's "compile outside
     isolate" and "`javac` outside isolate" become "in a box"; the compile budget gets a paragraph next to the
     evaluator budget; a "Judge conventions" bullet: **nothing a submission or an evaluator supplies runs or is
     read on the host, compilers included**, and artifacts come back only through `collect`.
   - `isolate_run`'s docstring: the `collect` rules, in the voice of the existing "Never reintroduce it" paragraph.
6. **Audit what already ran** (one-off, not committed): search stored sources for the patterns above, e.g.
   `SELECT id, user_id FROM submission WHERE source ~ '#\s*(include|embed)\s*["<]\s*/' OR source ~ '\.incbin'`,
   and the same over uploaded evaluators in storage. An include of a system header by absolute path is suspicious
   in itself; anything that names `/proc`, `/etc`, the storage root or the config is an incident. Do this before
   deploying, so the result is not muddied by attempts that now fail.
7. **C exercises plan**: once this lands, its "C compiles inside an isolate box" section and its spike step point
   here and keep only what is exercise-specific (flags, the harness, `ExerciseCompile`).

## Out of scope

- **Network.** isolate gives a box no network by default, and nothing here changes that; worth one assertion in the
  spike, not a feature.
- **What is in `/nix/store`.** The store is world-readable and already bind-mounted into every run box; a
  deployment that puts secrets there (a NixOS anti-pattern) has leaked them to every submission already. A line in
  the deployment notes, if there ever are any.
- **The web app.** It never compiles; it stores sources and hands them to the judge.
- **Per-language compile budgets** in `languages.toml`. One budget until a language shows it needs another.
