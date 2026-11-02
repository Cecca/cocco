---
title: Submissions and results
audience: everyone
order: 10
---

## Languages {#languages}

You hand in a single source file, and cocco works out the language from its
extension. There is no language to pick.

| Language | Extensions | Version | How it is built and run |
|---|---|---|---|
| Python (`python`) | `.py` | Python 3.12, with numpy | run with `python` |
| C (`c`) | `.c` | GCC 15.2 | `gcc -O2 -std=c17 -Wall`, linked with `-lm` |
| C++ (`cpp`) | `.cpp`, `.cc` or `.cxx` | GCC 15.2 | `g++ -O2 -std=c++17` |
| Java (`java`) | `.java` | OpenJDK 21 | compiled with `javac`, run with `java` |

The name in brackets is how the language is labelled next to your
submissions.

The file can be up to 1 MB and must be plain UTF-8 text. Your program reads
the test input from standard input and writes its answer to standard output.

In Java, name the file after its public class: `public class Main` goes in
`Main.java`, or the compiler refuses it. C programs can `#include "libfdi.h"`,
the input helpers used in the course notes, without uploading it.

## Submission status {#statuses}

A submission goes through three states, shown next to it in your list of
attempts:

- **queued**: it is waiting for the judge, the program that compiles and runs
  submissions.
- **running**: the judge is working on it, one test case at a time.
- **done**: every test case has been run, and the score is shown.

The page refreshes by itself, so you don't need to reload it. Most submissions
are done within seconds.

If a submission stays **queued** for a long time, the judge is either busy or
not running. It may be working through a long queue, for instance after your
teacher asked for every submission to be graded again, and exercises being
run in class always go first. If nothing moves for several minutes, tell your
teacher: no judge may be running at all. Your submission is not lost. It is
graded as soon as a judge is back.

A submission shown as **no tests** finished, but the challenge had no test
cases to grade it on.

## Reading your results {#results}

Click a submission to open its page. The top shows your file, the language and
when you handed it in. Below it is the source exactly as the judge received
it, then the results.

If your program didn't compile, a box headed "It did not compile" shows what
the compiler said, and every test scores zero without being run.

Otherwise there is one row per test case:

- **Verdict**: `success` means your program ran to the end and its output was
  scored. It doesn't mean the answer was right: look at the score for that.
  `timeout` means it ran out of time, `out_of_memory` that it used too much
  memory, and `runtime_error` that it crashed or ended with an error.
  `evaluator_error` and `judge_internal_error` mean something went wrong on
  the teacher's or the server's side; tell your teacher.
- **Score**: the points that test earned.
- **Time**: the CPU time your program used.
- **Memory**: the most memory it used at any moment.

Tests marked **sample** are the ones you can download from the challenge page.
On those you also see your program's output (stdout and stderr) and the
judge's message. See [Hidden test cases](/docs/submissions#hidden-outputs)
for why the others show less.

The score of a submission is the sum of its test scores. If the challenge has
a score to reach, a submission below it is marked failed. Your best submission
is the one that counts.

## Hidden test cases {#hidden-outputs}

Every test case that isn't a sample is hidden: you can't download it, and on
your submission page it shows only the verdict, the score, the time and the
memory. You
don't see what your program printed on them, or the judge's message.

That is on purpose. Your program's output could repeat the hidden input, and
the judge's message often quotes the expected output. Showing either would
give the test away.

To find out why a hidden test fails, look at its verdict, and at what your
program does on the samples. Try inputs of your own: very small, very large,
empty, or with unusual values. Compiler errors are always shown in full,
since they don't depend on any test.

Course staff see everything on every test.
