---
title: Challenges
audience: staff
order: 20
---

A challenge is one programming problem: a statement, a set of test cases, and
an evaluator that scores each run. Students hand in a source file, the judge
runs it on every test case, and their best attempt counts.

## Creating a challenge {#create}

Course staff create a challenge with **New challenge**, on the course page or
in the Challenges card of the course editor. The form asks for:

- a **Title**, which is how the challenge is listed on the course page;
- the **Problem statement**;
- how the **Leaderboard** ranks students, and the score that counts as a pass;
- the **Sandbox limits** each run gets.

**Create challenge** takes you to the editor, where you add the
[test cases](/docs/test-cases#add) and choose an
[evaluator](/docs/evaluators#default). A new challenge starts out hidden, so
students won't see it while you're still setting it up; you publish it from
the editor (see [Visibility](/docs/challenges#visibility)).

In the editor every card saves on its own: the statement, the leaderboard and
the limits each have their own **Save**, and the evaluator, the test cases and
the visibility take effect as soon as you act on them. A card with unsaved
changes says so, and the page warns you before you leave it.

Challenges are numbered in course order ("Challenge 01", "Challenge 02", …),
and you can reorder them from the course editor. The web editor has no field
for changing the title later. If you work on the challenge with `cocco push`,
the title in `challenge.toml` replaces the old one.

## Problem statement {#statement}

The statement is written in Markdown. On the left you type, and on the right
the preview updates as you go, rendered the same way the challenge page will
show it.

Besides the usual headings, lists, links and emphasis, you can use:

- **Code blocks** between lines of three backticks. Put the language name
  right after the opening backticks (`python`, `c`, `java`, …) to get syntax
  highlighting. A block with no language stays plain, which suits sample input
  and output.
- **Tables**, written with `|` between columns and a `---` line under the
  header row.

You don't need to repeat the limits or attach the samples in the text. The
challenge page shows the time, memory and thread limits above the statement,
and lists the public [sample files](/docs/test-cases#samples) below it, ready
to download.

## Leaderboard {#leaderboard}

A submission's score is the sum of the scores its
[evaluator](/docs/evaluators#contract) gave each test case, shown as the
evaluator wrote them. With the bundled evaluators each test is worth up to 1,
so a submission that gets 7 of 10 tests right scores 7.

**Score threshold** is the score a submission needs to pass. Graded
submissions below it are marked failed and stay off the leaderboard. The
challenge page tells students the target ("Reach N points to make the
leaderboard"). With the bundled evaluators, set it to the number of test cases
to require every test right.

Leave it empty and every graded submission counts as passed, wrong answers
included. The editor and your staff view of the challenge warn you when a
challenge has no threshold.

Students see a live top 10 next to the submit form. Each student appears once,
with their best submission, and only submissions that pass count. You choose
how the board is ordered with **Rank by**:

- **Running time**: the shortest running time summed over all test cases. The
  board is headed "Fastest solutions". Without a threshold, the fastest program
  wins whatever it printed.
- **Total score**: the highest score. Ties go to the faster submission. The
  board is headed "Top total scores".

Changing the ranking or the threshold re-sorts the board straight away, with
nothing to regrade: the threshold is applied whenever a page is shown, never
stored with the results. The ranking only affects what students see: your
[Class standings](/docs/challenges#standings) always rank by score. To see the
board itself, use [View as student](/docs/challenges#student-view).

## Visibility {#visibility}

Visibility decides when the class can see the challenge. Course staff always
can, so you can prepare and check everything before publishing. The card at the
top of the editor says where the challenge stands, and its buttons change it
straight away:

- **Hidden**: students don't see it on the course page and can't open it, even
  with a link. Every new challenge starts hidden.
- **Publish now** opens the challenge to the class at once.
- **Schedule** opens it by itself at the date and time you pick, with nobody
  needing to be there. A challenge that should open on Monday at 9:00 goes
  here. Until then the class sees its title and the opening time on the course
  page, but can't open it. The time is read in the server's time zone, the same
  one the pages print dates in.
- **Hide** takes it back off the students' page, or cancels a schedule.

Hiding a challenge that students have already submitted to takes it off their
page, but their attempts and scores are kept, which is why cocco asks you to
confirm. Publish it again and everything is back where it was.

If you want students to keep reading a challenge but stop submitting, archive
it from the editor's danger zone instead (see
[Archiving a challenge](/docs/challenges#archive)).

## Sandbox limits {#limits}

Every run of a student's program happens in a sandbox with these limits, once
per test case. Students see them as badges at the top of the challenge page.

- **Time (s)**: the CPU time the program gets for one test case. A program
  that waits instead of computing (sleeping, or blocked reading input that
  never comes) is stopped too, after twice the limit plus one second.
- **Memory (MB)**: the memory the program may use, interpreter included. The
  Python interpreter alone takes several MB before running a line of the
  student's code, so don't set this too low.
- **Threads**: how many processes or threads the program may run at once. Most
  challenges need 1. Java gets extra room on top of this for the JVM's own
  threads.

A run that goes over a limit scores 0 on that test case, with the verdict
`timeout` or `out_of_memory`. A program that crashes gets `runtime_error`. The
other test cases still run.

New challenges start at 5 seconds, 256 MB and 1 thread. Limits that are too
tight kill correct solutions, so try a reference solution first. Compiling and
running the evaluator have their own fixed budgets, so you never need to loosen
the students' limits for those.

Changing the limits doesn't rescore anything already submitted. Use
[Regrade all](/docs/challenges#regrade) for that.

## Class standings {#standings}

The standings on the challenge page show every student's best attempt:
students who passed come first, then higher scores, with the total running time
as the tie-break. When the challenge has a threshold, it is printed under the
score column, and attempts below it are badged failed. They refresh every few
seconds.

The table lists every student enrolled on the course, including those who
haven't handed anything in yet ("nothing handed in"), and anyone who has
submitted, even if they have since left the course. A spinner with "pending"
means some of that student's submissions are still waiting for the judge, and
"not graded yet" means none of their attempts has a score so far.

Click a row to see all of that student's submissions, with their results.

The standings always rank by score, whatever the
[Leaderboard](/docs/challenges#leaderboard) setting is. The leaderboard is what
students see; the standings are your view of the whole class.

## Regrading {#regrade}

**Regrade all** runs every submission to this challenge again. Use it after
you've fixed an expected output, changed the evaluator, added or retired test
cases, or changed the limits. None of those changes touch scores that are
already recorded.

Regrading deletes every recorded result for the challenge and puts every
submission back in the queue. They are then run on the **active** test cases
only. Two things follow:

- Scores on retired test cases are discarded for good. If you want to keep
  them, don't regrade.
- Students' current scores disappear until the judge has worked through the
  queue again. With many submissions this can take a few minutes, and
  exercises being run in class go first.

If the judge is running one of the challenge's submissions at that moment,
cocco refuses and changes nothing. Wait a few seconds and press the button
again.

After a regrade, no test case has graded anything yet, so they can all be
deleted again.

## Viewing as a student {#student-view}

**View as student**, at the top of the statement, shows the page your students
get: the statement, the submit form, the leaderboard and their own submission
history. **Back to the staff view** returns you to the standings.

Use it to check how the statement reads before you publish. The badges at the
top still say whether the challenge is hidden or when it opens, so you can tell
what students will see.

A file you submit here is a real submission. It is graded like a student's and
shows up in the class standings and on the leaderboard under your name.

## Reference solutions {#references}

A reference solution is your own answer to the challenge, added from the
**Reference solutions** card in the editor with a file and a label such as
"Naive O(n²)". Use it to show what a good answer scores and how fast it runs.
Leave the label empty to use the file name.

**Students see** says how much of it the class gets:

- **Staff only**: nothing. This is where a new reference starts.
- **Leaderboard**: its label, language and results, in a marked row.
- **Leaderboard and source**: the same, plus a link to the code.

A reference never takes a student's place. It has no rank, doesn't use one of
the top 10 slots, and on a tie the student comes first. It is listed only if it
would make the board, so a reference below the challenge's score target stays
off it, and the card warns you when that happens: either the reference or the
target is wrong. You always see every reference in the standings, marked as a
reference.

A reference is graded like a submission, but it is not re-run on its own. After
you change the test cases, a **stale** badge means the active test cases are no
longer the ones it was graded on; press **Re-run**. Regrading the challenge
re-runs references too.

You can change the label and who sees it at any time. To change the code, add a
new reference and delete the old one, so the results always belong to the code
shown. Delete is refused while the judge is running the reference; try again
when it finishes. A test case a reference has been graded on can't be deleted
until the reference is, just as for a student's submission.

## Archiving a challenge {#archive}

Archiving closes a challenge without taking it away. The class keeps reading
the statement, the samples, their own attempts and the leaderboard, but the
submit form is gone and no new submissions are accepted. On the course page the
challenge is greyed out with an "Archived" badge.

You can still edit an archived challenge, and **Unarchive** in the editor's
danger zone reopens it.

If the whole course is archived, its challenges are closed too, whatever their
own setting says. Unarchive the course to reopen them.

Archiving is different from hiding: a hidden challenge disappears from the
students' page, while an archived one stays readable. See
[Visibility](/docs/challenges#visibility).

## Deleting a challenge {#delete}

**Delete challenge…** removes the challenge for good: every test case and its
files, the evaluator, every submission anyone ever made to it, and every
score. There is no undo. If you only want to stop submissions, archive it
instead.

To confirm, type the challenge's title exactly as shown and press **Delete for
good**. You then land on the course editor.

If the judge is running one of the challenge's submissions at that moment, the
delete is refused and nothing changes. Try again a moment later.
