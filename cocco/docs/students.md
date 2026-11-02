---
title: For students
audience: student
order: 10
---

## Joining a course {#enrolling}

Your home page lists the courses you are on under **Your courses**, and every
other course under **Other courses**. To join one, press **Enroll!** on its
card or on the course page. You join as a student, and the course's
challenges appear straight away.

Some courses need an **enrollment key**. Their card says **Enroll…** instead,
and the course page asks for the key. Your teacher gives it out, in class or
on a slide. Type it exactly as given, capitals included. If you get "That key
is not right.", check it with your teacher.

An archived course is finished and doesn't take new students.

You can't leave a course by yourself. If you joined the wrong one, ask its
teacher to remove you.

## Handing in a solution {#submitting}

Open a challenge from the course page. On the left is the statement, with the
time and memory limits at the top and the sample files below it. Download the
samples and try your program on them first.

To hand in, drag your source file onto **Submit a solution**, or click
**browse**, then press **Run & submit**. cocco tells the language from the
file extension (see [Languages](/docs/submissions#languages)). Your program
reads the input from standard input and prints the answer on standard output,
with nothing else: no prompts such as "Enter a number:".

You can hand in as many times as you like. Each attempt is graded on its own,
and your best attempt is the one that counts, so a worse attempt later never
lowers it. If the challenge page says "Reach N points to make the
leaderboard", attempts below that are marked failed.

A challenge that is **Archived** is closed: you can still read it, your
attempts and the leaderboard, but you can't hand in anything new.

## Your submissions {#reading-results}

**Your submissions**, at the bottom of the challenge page, lists every attempt,
newest first. It updates by itself while the judge works.

Each row shows the attempt number, your file, when you handed it in, the total
running time and the result. The result is **queued** or **running** while
the judge is busy with it, then your score: the points summed over all tests.
When the challenge has a score to reach, the badge is green if you reached it
and red, marked "failed", if you didn't. Hover over it to see how many tests
passed.

Your best attempt is highlighted and marked **Best · counts**: one that reached
the target first, then the highest score. If two attempts tie, the later one
counts.

Click a row to see that attempt's results test by test. See
[Reading your results](/docs/submissions#results) for what each column means,
and [Hidden test cases](/docs/submissions#hidden-outputs) for why some tests
show less than others.

## The leaderboard {#leaderboard}

Next to the submit form is the challenge's leaderboard: the top 10 of the
class, with each student's single best submission. Only attempts that reach
the challenge's score target, if it has one, make the board. Your own row is
highlighted and starts with "You". It updates by itself every 15 seconds.

The heading tells you how it ranks:

- **Fastest solutions**: by total running time over all tests.
- **Top total scores**: by the points scored across all tests. A tie goes to
  the faster submission. The leaderboard
shows your display name and picture, which you can change on your profile
(see [Your profile](/docs/account#profile)).

## Exercises in class {#exercises-in-class}

Your teacher may run short programming exercises during the lesson. Open the
course page and pick your group's session under **Exercise sessions**.

The numbered buttons at the top are the exercises you can open: green when
you have solved one, amber when you have checked it but not solved it yet.
The greyed-out numbers after them are locked. Your teacher unlocks them as the lesson goes on, and
if you open one too early you get "Wait for the teacher", which opens the
exercise by itself once it is unlocked.

Write your code in the editor on the right. There are two buttons:

- **Run** runs your program on input you type in the box, and shows what it
  printed. Use it to try things out.
- **Check** runs it on the exercise's tests and tells you which pass. An
  exercise is solved once a Check passes every test.

The **Tests** are listed under the statement, with their input and the
expected output. Some exercises have hidden tests too: Check tells you whether
they pass, but not what they are. Everything your program prints is compared,
so don't print a prompt before reading input.

If you are stuck, **Show a hint** gives you the exercise's hints one at a
time, when it has any. Your code is saved in your browser as you type, and
**Start over** puts the starter code back. Exercises are practice: nothing is scored or ranked. When
your teacher reveals a solution, it appears under the statement. After the
session is closed you can still open your exercises and see your last code and
results, but not run anything new.
