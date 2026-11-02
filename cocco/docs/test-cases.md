---
title: Test cases
audience: staff
order: 25
---

## Adding a test case {#add}

A test case is a pair of files. The **input file** is fed to the student's
program on its standard input, and whatever the program prints on its standard
output is compared with the **expected output file** by the challenge's
[evaluator](/docs/evaluators#default). Each test case is scored on its own, and
a submission's score is the sum over the test cases it ran on. Adding or
retiring a test case therefore changes the score a full solution reaches:
update the [score threshold](/docs/challenges#leaderboard) to match.

Under **Add a test case** in the editor, pick the two files, tick **Public
sample** if students should be able to download them, and press **Add**. The
new test case goes to the end of the list, and every submission the judge runs
from then on is graded on it.

A few rules about the files:

- The input and the expected output must be two different files.
- File names must be unique within the challenge, retired test cases included.
  Unusual characters in a name are replaced by `_`.
- Each file can be up to 32 MB (see
  [Large test files](/docs/test-cases#large-files)).

You can rename either file with the ✎ next to its name. Only the name changes;
the contents and any scores stay as they are.

Adding a test case doesn't grade the submissions already handed in. Use
[Regrade all](/docs/challenges#regrade) on the challenge page if you want them
graded on it.

## Public samples {#samples}

A test case marked **Public** is a sample. Students can download its input and
expected output from the challenge page, listed under the statement as
`sample1_input.txt`, `sample1_output.txt` and so on, so they can test their
program before handing it in.

On their submission page, students see the full result of a sample: the
verdict, score, time and memory, plus what their program printed (standard
output and standard error) and the evaluator's message. On a test case that isn't public they only see the
verdict, score, time and memory. Showing the output would give the hidden test
data away, since a program can echo its input and an evaluator like Line diff
prints the expected output in its message. You always see everything.

Ticking or unticking **Public** takes effect immediately and doesn't change
any scores. A retired test case is never offered for download, even if it is
still ticked as public.

## Active and retired test cases {#active}

Only **active** test cases are graded. Untick **Active** to retire a test case:
new submissions skip it, and it stops being downloadable even if it is marked
public. The row is dimmed but stays in the list, with its files and every
score already recorded on it, so you can tick it again to bring it back.

Retiring is how you stop using a test case that has already graded
submissions. Those test cases can't be deleted (their ✕ is greyed out),
because deleting one would leave scores that refer to nothing. A test case
nothing has been graded on can be deleted with ✕, which also removes its two
files.

Retiring doesn't change existing scores: earlier submissions keep the result
they got on that test case. To rescore everything on the active test cases
only, use [Regrade all](/docs/challenges#regrade). That discards the results
on retired test cases for good.

## Order {#order}

Test cases run in the order of the list, and results are shown in the same
order on the submission page. Move a test case with ↑ and ↓ in the **Order**
column.

The test numbers students see, on the sample downloads and on the submission
page, follow this order too. A common layout is the public samples first, then
the hidden ones from small to large.

Every test case runs whatever happens on the earlier ones, so the order never
changes a score.

## Large test files {#large-files}

Test files can be large. Each file you add in the editor can be up to 32 MB,
and it is stored compressed, so a big input costs less disk space than its
size suggests.

If you have many large files, `cocco pull` and `cocco push` let you manage a
challenge as a folder on your computer and upload all its test cases at once.
A push can carry up to 256 MB, counted before compression.

Large inputs need realistic [sandbox limits](/docs/challenges#limits): reading
a few megabytes of input takes time, especially in Python. Try a reference
solution on your biggest test case before setting the time limit.

What a program prints is passed to the evaluator in full. Only the copy that
is stored and shown on the submission page is cut short, after about 80,000
characters.
