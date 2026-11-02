---
title: Evaluators
audience: staff
order: 30
---

## The default evaluator {#default}

The evaluator is the program that turns a submission's output into a score,
once per test case. Each challenge has one.

Until you choose one, a challenge uses **Exact match**, shown in the editor as
"Default — exact match". A test case gets full marks when the program's output
equals the expected output, and zero otherwise. Whitespace at the very start
and end of the output is ignored, so a missing final newline doesn't matter.
Anything else counts, including a stray space in the middle of a line. A
student who gets it wrong sees "The output is not the expected one."

Exact match suits most challenges with one right answer. Pick another when:

- the answer can be partly right and should earn partial credit (a
  [bundled evaluator](/docs/evaluators#bundled) such as Line diff, Accuracy or
  Macro-F1);
- there are several correct answers, or numbers that only need to be close
  enough (an evaluator you write yourself; see
  [Writing an evaluator](/docs/evaluators#contract)).

The evaluator only runs when the program finished within the
[sandbox limits](/docs/challenges#limits). A run that timed out, ran out of
memory or crashed scores 0 on that test case without reaching it.

Changing the evaluator doesn't rescore anything already submitted. Use
[Regrade all](/docs/challenges#regrade) on the challenge page for that.

## Writing an evaluator {#contract}

An evaluator is an ordinary program, in any language cocco runs (see
[Evaluator languages](/docs/evaluators#languages)). It is run once per test
case, in the same sandbox as the students' programs, with three file paths as
its arguments:

```
evaluator <actual_output> <expected_output> <test_input>
```

1. the output the student's program printed;
2. the test case's expected output file;
3. the test case's input file.

Read the files from these paths. Standard input is empty, and the file names
are not meaningful.

It must print **one JSON object** on standard output and exit with status 0:

```json
{"score": 0.75, "message": "3 of 4 lines correct."}
```

- `score` is the points this test case earns, as a finite number. Pages show
  it as written, and a submission's score is the sum over its test cases. The
  bundled evaluators give each test up to 1, but any scale works as long as
  higher is better: the leaderboard and the
  [score threshold](/docs/challenges#leaderboard) both assume it.
- `message` is optional text explaining the score. Students see it only on
  [public samples](/docs/test-cases#samples); you always see it.
- Any other keys are ignored.

A wrong answer is a normal result: exit 0 with a score of 0. Exiting with
another status, crashing, printing anything that isn't a single JSON object,
running for more than 10 seconds or using more than 1 GB of memory all count
as the evaluator failing. That test case then gets the verdict
`evaluator_error` and a score of 0, with a message that tells you what went
wrong. Print debugging output to standard error, not standard output.

Don't trust the student's output. It can be empty, huge, not valid UTF-8, or
not what you expected at all, and that should end in a low score rather than a
crash.

### Example {#contract-example}

This Python evaluator accepts numbers that are within 10⁻⁶ of the expected
ones and gives partial credit for each correct number:

```python
import json
import sys

actual_path, expected_path, input_path = sys.argv[1:4]

with open(actual_path, encoding="utf-8", errors="replace") as f:
    actual = f.read().split()
with open(expected_path, encoding="utf-8") as f:
    expected = f.read().split()

if len(actual) != len(expected):
    score = 0.0
    message = f"Expected {len(expected)} numbers, got {len(actual)}."
else:
    try:
        close = sum(abs(float(a) - float(e)) <= 1e-6 for a, e in zip(actual, expected))
        score = close / len(expected) if expected else 1.0
        message = f"{close} of {len(expected)} numbers are correct."
    except ValueError:
        score = 0.0
        message = "The output contains something that is not a number."

print(json.dumps({"score": score, "message": message}))
```

The [bundled evaluators](/docs/evaluators#bundled) are written the same way,
and you can download any of them as a starting point.

## Bundled evaluators {#bundled}

cocco comes with four evaluators. Under **Start from a bundled evaluator**,
pick one and press **Copy in** to make it this challenge's evaluator.

- **Exact match** (`equality.py`): 1 if the output equals the expected output,
  ignoring whitespace at the start and end, and 0 otherwise. This is the
  default.
- **Line diff** (`diff.py`): the fraction of expected lines that appear
  unchanged in the output, so it gives partial credit. The message is the diff
  itself, which shows the expected output, so students see it only on public
  samples.
- **Accuracy** (`accuracy.py`): reads both files as one label per line and
  scores the fraction of lines that match exactly. Lines the output is missing
  count as wrong. For classification challenges.
- **Macro-F1** (`f1.py`): reads both files as one label per line and scores
  the average of the F1 score of each class, so every class weighs the same
  however rare it is. For classification with imbalanced classes, where
  accuracy flatters a program that always predicts the most common label.

Copying makes a copy of the script for this challenge. Press **Download** to
get it, edit it, and [upload](/docs/evaluators#upload) your version. A copied
script stays as it is when cocco is updated, while a challenge on the default
always uses the latest Exact match.

## Uploading an evaluator {#upload}

Under **Upload an evaluator**, choose your script and press **Upload**. From
then on it grades every submission the judge runs. It replaces the current
evaluator, and a script you uploaded earlier is deleted.

The file must be:

- a source file whose extension says what language it is in (see
  [Evaluator languages](/docs/evaluators#languages));
- UTF-8 text, and at most 1 MB.

The editor then shows the file name and the language it will run as.
**Download** gives you the current evaluator, the default one included, and
**Reset to default** goes back to Exact match and deletes the uploaded script.
Students can never download the evaluator.

The editor doesn't run your evaluator when you upload it. A mistake, such as a
compile error or output that isn't JSON, shows up when the next submission is
graded: every test case gets the verdict `evaluator_error`, with the reason in
the message. To check a new evaluator, submit a reference solution yourself
from [View as student](/docs/challenges#student-view) and read the results.

Uploading doesn't rescore earlier submissions. Use
[Regrade all](/docs/challenges#regrade) once you're happy with it. See
[Writing an evaluator](/docs/evaluators#contract) for what the script must do.

## Evaluator languages {#languages}

You can write an evaluator in any language students can submit in. The file
extension decides the language:

- Python: `.py` (numpy is available)
- C: `.c`
- C++: `.cpp`, `.cc` or `.cxx`
- Java: `.java`

C, C++ and Java evaluators are compiled with the same compilers and options as
submissions, once per submission and inside the sandbox. If the evaluator
doesn't compile, every test case gets `evaluator_error`, with the compiler's
first lines in the message. Students see that message on public samples, so
fix it promptly.

In Java, name the file after its public class: `public class Checker` goes in
`Checker.java`. The three paths arrive as `args[0]`, `args[1]` and `args[2]`.
In C and C++ they are `argv[1]` to `argv[3]`.

Compiling has its own budget, and running the evaluator gets 10 seconds and
1 GB of memory per test case, whatever the challenge's
[sandbox limits](/docs/challenges#limits) are.
