# Code Execution Design

## Sandbox: isolate

The sandbox for running untrusted submissions is [**isolate**](https://github.com/ioi/isolate), the tool used by IOI and ICPC. It provides strong isolation via Linux namespaces and cgroups, with built-in support for time and memory limits, stdin/stdout redirection, and termination on violation.

Isolate handles sandboxing only — it has no concept of programming languages.

## Language runtimes: Nix flakes

Language runtimes are managed via a **single Nix flake** with one package output per supported language (e.g. `python`, `gcc`, `jdk`). This gives pinned, reproducible environments managed entirely in code.

Since all Nix binaries live under `/nix/store`, the store is bind-mounted read-only into the isolate sandbox:

```bash
isolate --box-id=1 \
  --dir=/nix/store:ro \
  --stdin=/path/to/input.txt \
  --run -- /nix/store/<hash>-python3-3.11/bin/python3 solution.py
```

The exact store path for each runtime is resolved at startup — every runtime `languages.toml` names, before the judge grades anything — and held for the life of the process, keeping execution fully reproducible and tied to `flake.lock`. Each build is made with `--out-link`, so the store path gets a GC root and cannot be collected out from under a running judge.

### NixOS setuid note

Isolate requires a setuid binary. On NixOS this is declared via `security.wrappers`. On plain Linux with Nix, isolate should be installed outside Nix (via the system package manager or manual build) while Nix manages only the language runtimes.

## Execution flow

For **compiled languages** (C, C++, Java, ...):
1. Compile in its own isolate box, and collect the artifacts back out. Compiling is not a trusted operation: a compiler reads whatever the source names (`#include "/etc/passwd"`, `.incbin`, `#embed`, javac's `.` class path), and the diagnostics or the binary would hand it back.
2. Run the resulting binary inside isolate.

For **interpreted languages** (Python, ...):
1. Run the interpreter directly inside isolate, with the source file copied into the sandbox working directory.

## Evaluation

Grading is a **program, not a plugin**. Each challenge names a script that course staff uploaded; the judge compiles it once per submission and runs it once per test case, down the same isolate + Nix path as the submission itself, handing it three paths on argv (actual output, expected output, test input) and reading one JSON object off its stdout.

The rejected alternative was to keep evaluators as Python classes inside the judge process. Two problems: a teacher who needs a special judge — several valid answers, a domain metric, checking a solution against the instance — would have to patch and redeploy the judge, and every checker would have to be Python. Running them in-process also means teacher code with no CPU or memory bound in the middle of the judge loop; the same `difflib` checker that used to be able to hang the judge is now simply killed by isolate and reported.

That this is safe rests on isolate's defaults as much as on its limits: a box gets a fresh network namespace unless `--share-net` is passed, so an uploaded checker cannot reach the network, the database, or anything else the judge host can see.

## Test case inputs

Test inputs can be several megabytes. They are stored on the **filesystem** (not in the database), with only the file path recorded in the database.

They are also **gzip-compressed on disk, transparently**. A million-line input is line-oriented ASCII, which is the shape that compresses best, and a course accumulates hundreds of them; nothing outside `cocco/storage.py` knows. There is deliberately no `.gz` suffix: the path in the database is the name the teacher uploaded and the name the transfer format uses, so `resolve`, `exists`, `rename`, `delete` and `plan_testcases` are untouched, and only the codec moves. Uncompressed roots are not a second case to support — `cocco-migrate compress-storage` converts one, and both servers refuse to start until it has been run, the way they refuse a database that is behind its migrations.

The judge draws the boundary: **storage is compressed, an isolate box is plain**. A submission's standard input is inflated straight into the pipe isolate is reading, so a huge input is still never written out whole — the one thing that changed is that stdin is a pipe rather than the file's own descriptor. The evaluator's two files are expanded directly into the box, which is the copy isolate was already making. Nothing an evaluator or a submitted program opens is ever compressed, so the checker contract is exactly what it was.

## Alternatives considered

| Option | Verdict |
|---|---|
| Judge0 | Dropped: stateless HTTP API is a poor fit for multi-MB inputs |
| Docker + gVisor | Viable but heavier; container startup latency requires pre-warming |
| Firecracker microVMs | Strongest isolation, but operationally complex for a self-hosted setup |
| Piston | Easy to bootstrap, but less control and less battle-tested for adversarial input |
| Storing inputs in DB | Dropped: multi-MB blobs harm query performance, backup size, and I/O |


# Database design

Instead of relying on an ORM (like `SQLModel` of `SQLAlchemy` ORM's API)
we rely on SQLAlchemy 2.0 Core API, that give more control over the queries.
In fact, it is very rare that the database returns full rows of a single table: rather the common case is for the database to give rows with results from many tables.
To get structured data out of the database (thus reducing the number of queries and offloading the work to the database, that is more efficient at handling it) we take inspiration from this blog post: https://www.scattered-thoughts.net/writing/sql-needed-structure/


# Interactive exercises

Short Python exercises, run live in class: the teacher unlocks them one at a time and watches who
has solved what. They began as `esercizi-python`, a static site that ran everything in the browser
with Pyodide. Three decisions shaped the move into cocco.

## The judge, not Pyodide

The prototype's README names its own two limits. **Hidden tests can be decoded**, because a page
cannot check a program against a test without holding the test. **"Solved" can be forged**,
because it is whatever the page's localStorage claims. Both are fine for self-study. Neither is
fine once a teacher is watching a dashboard of who has solved what, and the dashboard was the point
of the move. So Run and Check both go to the judge, through the same isolate + Nix path a
submission takes. Hidden tests and unrevealed solutions never leave the server, and "solved" is a
verdict the server reached.

The cost is latency and load: a click becomes a round trip through a queue. It was measured before
anything was built. One `print(42)` through isolate costs about 35 ms. The judge as it was then (one
submission at a time, tests in sequence, a 5 s idle poll) needed 40 s to drain 100 five-test
submissions, which would have left the last student waiting that long. The judge therefore
learned three things, and challenges benefit from them too: concurrent workers, sized against the
CPU; `LISTEN`/`NOTIFY`, so a job wakes it at once; and a strict priority, so that a regrade cannot
hold up a lesson. On the target load (100 students pressing Check within 10 s) the p95 from click to
verdict was 0.56 s on a laptop, against a 3 s target. Most of that is the 500 ms poll.

Reusing one box for all of a job's tests was measured too, and not built. It would save 15–25 % of
a job, while concurrency saved about 9×. It would also cost isolation between tests, since a file
one test writes would still be there for the next.

Two of the prototype's semantics are about Python rather than about bytes on stdout: an
`input("…")` prompt is not output, and `error: ValueError` must also accept a subclass. Both
therefore need code running *inside* the student's process. A small bundled harness runs the
program and reports what it did as one line of JSON. The comparison of outputs stays in the judge
process: the rules are declarative and the code is ours, so a sandbox per comparison would double
the cost to buy nothing.

Run is the one thing given up. It is no longer interactive: stdin is typed into a box up front, and
the harness echoes what was read so the transcript still reads like a terminal.

## Not a kind of challenge

A challenge is built for large inputs on disk, evaluator programs, resource limits, scores and
leaderboards. An exercise is a few short inputs, starter code, hints and a declarative comparison
rule, and it is graded by nothing. Making exercises a kind of challenge would have put a "but not
for exercises" branch into every standings, leaderboard, transfer and regrade path. So they are
tables of their own, and they share the **execution layer** (`Language`, `isolate_run`, the box pool,
the prewarmed runtimes) and nothing above it.

That is also why their tests live **in the database**, against the rule for test case inputs above.
That rule exists for multi-megabyte inputs. An exercise's inputs are a few bytes typed into a
Markdown file, and keeping them in the database lets the judge read an exercise's tests in one
query and keeps `Storage` out of the feature entirely.

## Compiled versions, not exercise rows

A set is authored as one Markdown file, and the server stores that text verbatim. There are no
`exercise` or `exercise_test` tables: storing the parsed fields beside the source would be two
copies of the same facts, and the first route that wrote one and not the other would make them
disagree in silence.

Instead, each import creates an **immutable version**: the source, plus a compiled document (the
parse, plus the expected outputs the judge computes by running the author's solution) as one JSONB
value. The source is written when the version is created. The document and its status are written
exactly once, by the build that finishes it. Values that are written once and never updated cannot
drift apart, so no rule has to keep them in step.

Everything else follows from that:

- A failed re-import breaks nothing. The set keeps pointing at its last good version.
- A session pins a version, so its content does not change under a class mid-lesson. Moving it to
  a fixed version is an explicit button, which checks that exercises still line up by position.
- A check records the version it ran against, so its result is always read against the tests it
  was graded on.
- Re-export is lossless, and the transfer CLI moves a set as one file.
- The solution is on the server, so it can be revealed to the class after an exercise.

Old versions are a few kilobytes each and are kept.
