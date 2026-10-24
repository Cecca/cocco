---
name: cocco-exercises
description: Write a cocco exercise set - the one Markdown file a teacher imports into cocco to run short programming exercises live in class (Python, C or C++). Use when asked to create, extend, translate or fix a cocco exercise set, or "an exercises.md" for cocco, from a list of exercises, a topic or an existing lab sheet.
---

# Writing a cocco exercise set

A cocco exercise set is **one Markdown file**. The teacher imports it on the course page
("Import a set"), the judge runs every solution to compute the expected outputs, and the set is
then run in class. Your job is to turn what the teacher asked for into that file, correct the
first time: a file with an error in it is refused on import, and a solution that crashes fails
the build.

## Workflow

1. **Read the request.** Note the exercises asked for (or the topic and how many), the
   language, the level of the students, and anything about tests, hints or starters. If the
   request names a language that is not supported (see *Languages*), say so and stop. If it is
   silent on details, choose sensibly rather than asking: Python, 3-6 tests per exercise with
   1-2 of them hidden, one hint each.
2. **Write the file** in the format below, one `# Heading` per exercise, in the order asked.
   Name it after the set (`loops.md`), or `exercises.md` if it lives in a `cocco pull`
   directory.
3. **Check it** against the checklist at the end. If a Python interpreter or a C compiler is at
   hand, run every solution on every one of its inputs and confirm it prints what the
   statement promises and never crashes (except where an `error` block says it should).
4. **Tell the teacher** what you wrote: the exercises, one line each, and anything you decided
   for them. Then how to load it: on the course page, *Exercises → Import a set*, or on an
   existing set's page, *Import a new version*. With the `cocco` command line tool,
   `cocco push --set ID` inside a pulled directory imports it and prints what the build found.

## The format

````text
---
title: Loops and lists                 required: the set is listed by it
description: Lab of 14 October          optional
language: python                       optional; default python
---

Optional introduction, in Markdown, shown before the first exercise.

# Sum of a line                        each "# Heading" starts an exercise

The statement, in Markdown. Any code block that is not one of the
blocks below stays part of the statement, so examples are fine.

compare: numbers 0.01                  optional, see "Comparing output"
time: 2                                optional, seconds per test, default 5, at most 120

```starter                             optional: what the editor starts with
```solution                            required: never shown to students
```input                               a visible test: its stdin
```input hidden                        a hidden test
```output                              optional, right after an input
```error                               optional, right after an input (or its output)
```hint                                optional, one block per hint, any number
````

Rules worth knowing by heart:

- **Only a level-one heading (`# Title`) starts an exercise.** Use `##` and below freely inside
  a statement. Nothing but the front matter and the introduction may come before the first
  `#`.
- **Keywords are English and spelled exactly as above.** `compare:` and `time:` are lines of
  their own inside the exercise, outside any code block.
- **A role block may carry the language's name in front**: `python solution`, `py starter`,
  `c solution`, `cpp input`. A block marked with *another* language's name (`python solution`
  in a C set) is an error, because left alone it would be shown to students as statement text,
  solution and all.
- **Every exercise needs a `solution`.** The judge runs it on each input to compute the
  expected output, twice, to catch a solution whose output changes from run to run. Do not
  write `output` blocks unless the teacher hands you exact expected outputs: a computed one
  can never be wrong.
- **An exercise with no `input` block gets one test with empty input.** That suits "print a
  triangle of stars"; anything that reads input should have several tests.
- **Tests are separate runs**: each `input` is the whole stdin of one run of the program.
- **`input hidden`** (the word `hidden` after `input`): the student sees only whether a hidden test passed, never its input or
  output. Keep at least one test visible (every test hidden is a warning), and use hidden
  tests for edge cases a student should think of without being told: empty input, zero,
  negative numbers, a single element, ties.
- **`hint` blocks are Markdown**, shown one at a time on request. Make the first a nudge and
  any later one more explicit; never paste the solution into a hint.
- **The `starter`** is the editor's initial contents: the reading of the input, a function
  signature, the `#include`s and `main`. It should not already pass the tests (that is a
  warning) and should leave the actual work to the student. Leave it out for a blank editor.
- **The `title:`** is required; there is nothing else in the front matter except
  `description:` and `language:`.

### No prompts

Everything the program prints is compared with the expected output, prompts included. So
solutions and starters **never pass a prompt**: `input()`, not `input("Enter n: ")`; no
`printf("n? ")` before a `scanf`. Say what the input looks like in the statement instead
("The input is one line with two integers.").

### Errors

An `error` block after an input says that the program must **end with that error** on that
input. It holds one word:

- in Python, the exception's class name, matched exactly (`ValueError`, `ZeroDivisionError`,
  `IndexError`). There is no subclass matching: `LookupError` does not accept an `IndexError`;
- in C and C++, the signal the program dies of: `SIGFPE` (integer division by zero),
  `SIGSEGV` (bad memory access), `SIGABRT` (a failed `assert` or `abort()`);
- or nothing (an empty block): whatever error the solution ends with.

The solution itself must end with that error on that input, or the build fails. An `error`
block may follow an `output` block on the same input, when the program prints something before
it fails. Use these for exercises *about* errors ("raise `ValueError` when the list is
empty"); an ordinary exercise has none.

### Comparing output

By default trailing spaces on each line and trailing blank lines are ignored, and everything
else must match. A `compare:` line changes that for one exercise, with rules separated by
commas:

| rule           | effect                                                                   |
| -------------- | ------------------------------------------------------------------------ |
| `exact`        | character for character, trailing spaces included                        |
| `spaces`       | runs of spaces count as one, and leading spaces are ignored              |
| `case`         | upper and lower case are equivalent                                      |
| `numbers 0.01` | numbers may differ by the tolerance (default `1e-6`); a decimal comma is fine |
| `order`        | the lines may come in any order                                          |

Use `numbers` whenever the output has floating point results, and `order` when the statement
lets the order of the lines vary (the elements of a set, the keys of a dictionary). Prefer
telling students the exact format to print over loosening the comparison.

## Languages

Set `language:` in the front matter; one set is one language.

- **`python`** (the default): Python 3, standard library only. Programs read stdin with
  `input()` or `sys.stdin` and print to stdout. Hash seeds are fixed, so printing a `set` gives
  the same order on every run, but do not rely on it in statements: sort before printing.
- **`c`**: compiled with `gcc -O2 -std=c17 -Wall ... -lm`. Read with `scanf`/`fgets`,
  `return 0` from `main`.
- **`cpp`**: compiled with `g++ -O2 -std=c++17`. Read with `std::cin`.
- **`java`** is not supported for exercises yet. Say so and offer Python, C or C++.

Each test runs alone, with the exercise's time limit (default 5 seconds), 256 MB of memory and
256 KB of output, and no network or files other than stdin. Nothing random: a solution that
prints something different on the second run is flagged.

## Writing good exercises

- **One idea per exercise**, and order the set from easy to hard: the teacher unlocks the
  exercises one at a time in class.
- **The statement says exactly what to read and what to print**, with one worked example: the
  first visible test usually is that example, so show its input and output in the statement
  as plain code blocks (they stay statement text).
- **Tests cover the statement**: the example, an ordinary case, then the edges (smallest input,
  empty, zero, negatives, duplicates, the largest the statement allows). Three to six tests is
  typical. Avoid tests so large they need more than a fraction of a second.
- **Keep the input format simple**: one value per line, or space-separated values on a line,
  each said in the statement.
- **Write the solution the way a good student would**, using only what the course has taught
  at that point; students may see it once the teacher reveals it.

## Checklist before handing the file over

- [ ] The front matter is first, between two `---` lines, with a `title:`.
- [ ] Every exercise starts with `# Title` and has a `solution` block.
- [ ] Every fenced block is closed, and no role block carries another language's name.
- [ ] No solution or starter prints a prompt.
- [ ] Every solution handles every one of its inputs; if it must fail, an `error` block says how.
- [ ] Every solution prints the same thing every time it runs.
- [ ] At least one visible test per exercise; edge cases are hidden tests.
- [ ] Floating point output has a `compare: numbers` line.
- [ ] No hint gives the solution away.

## Examples

`examples/python.md` and `examples/c.md`, next to this file, are complete sets that import
cleanly. Read the one for the language you are writing before you start.
