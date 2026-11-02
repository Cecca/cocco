---
title: Exercises in class
audience: staff
order: 35
---

## Exercises and challenges {#what}

Exercises are short programming tasks for class time, in Python, C or C++.
Students write their code in an editor on the page, try it with **Run** on
input of their own, and press **Check** to run it on the exercise's tests. You
open a **session** of a set for a group, unlock the exercises one by one as the
lesson goes on, and watch the class's progress live.

Exercises are practice, not assessment. Nothing is scored, ranked or kept in
the [Class standings](/docs/challenges#standings). A student has either
solved an exercise (some Check passed every test), attempted it, or not tried
it yet, and that is all the progress cocco keeps.

Challenges are the opposite: a problem students work on in their own time,
handing in a source file whenever they like, with every test scored and their
best attempt counted (see [Creating a challenge](/docs/challenges#create)).

Use a challenge for anything you grade or rank, or anything that needs large
test files and real time limits. Use exercises for the ten-minute tasks you'd
put on a slide and walk through together, where you want to see who is stuck
and decide when the class moves on.

## The exercise file {#format}

A set of exercises is one Markdown file that you write and import. A short
header gives the set its title and its language, and each `# Heading` starts
an exercise: its statement, then blocks for the starter code students begin
from, your solution, the test inputs, and any hints. You don't have to write
the expected outputs: cocco runs your solution to compute them.

The format is written down in full in the exercise-writing skill, which the
**Import a set** box on the course page offers for download:
[Download the exercise-writing skill](/skills/cocco-exercises.zip). It is a
zip meant for an AI assistant: unzip it into the assistant's skills folder
(`~/.claude/skills/` for Claude Code) and ask for the exercises you want. Its
`SKILL.md` is plain text, so you can also read it and write the file yourself.

A set that has already been imported is a working example too: **Download**
in its **Versions** list gives you back the file as it was imported.

## Importing a set {#import}

On the course page, under **Import a set**, choose the `.md` file and press
**Import**. The file must be UTF-8 text and at most 256 KB.

cocco reads the file straight away. If it has errors, nothing is imported and
you get the list of problems, each saying where it is. Fix the file and import
it again. Otherwise you land on the set's page, and the judge **builds** the
new version: it runs your solution twice on every test input to compute the
expected outputs, and runs the starter code once.

The **Latest import** card shows the build as it goes, from "building" to
"ready" or "failed", with every problem the build found, errors first:

- **Errors** fail the build. A solution that crashes, runs out of time, doesn't
  compile, or doesn't stop with the error the file says it should, is an error.
- **Warnings** don't. A starter that already passes the tests or doesn't
  compile, a solution that prints something different on its second run, an
  exercise whose tests are all hidden: all of these still build, but are worth
  a look.

On a version that built, each problem links to that exercise in the
[preview](/docs/exercises#preview). The set is named after the file's
`title:`, or after the file name if it has none.

Students see nothing of a set until you
[open a session](/docs/exercises#open-session) of it.

## Versions {#versions}

Every import of a set's file is a new **version**, and a version never
changes once it is built. To fix a typo or add an exercise, edit your file and
use **Import a new version** on the set's page. It goes through the same check
and build as the [first import](/docs/exercises#import).

The **Versions** list shows every import, newest first. The one marked
**current** is the newest that built without errors, and a new session is
opened on it. A new version only becomes current once its build is ready, so a
file that fails to build changes nothing: the set and its sessions keep the
version that worked.

Sessions already open stay on the version they were opened on, even when a
newer one becomes current. Students never see their exercises change in the
middle of a lesson unless you move the session yourself (see
[Switching version](/docs/exercises#switch-version)).

For each version, **Preview** lets you try it as a student would, and
**Download** gives you the file it was imported from.

## Sessions {#sessions}

A session is one run of a set with one group of students: lab group A on
Monday, group B on Wednesday. Each session has its own pacing, its own
revealed solutions and its own record of who solved what. Code doesn't carry
over from one session to another.

Students find the open sessions under **Exercise sessions** on the course
page, labelled as you named them, and pick theirs. Any student on the course
can open any session, so give each a label the class recognises. Closed
sessions stay listed there, folded under "Closed sessions", for students to
look back at their work.

A session is one of two kinds:

- **Live**: you unlock the exercises one at a time (see
  [Unlocking exercises](/docs/exercises#unlocking)). For a lesson.
- **Open**: every exercise is unlocked from the start. For homework.

The session's page is where you run it. Its **Dashboard** shows each
student's progress, and its **Projector** is the screen to show the class.

A session stays on the version it was opened on, and stays in that version's
language.

## Opening a session {#open-session}

Under **Open a session** on the set's page, type a **Label** (such as "Lab A
— 20 Oct"), choose **Live** or **Open**, and press **Open session**. You land
on the session's page. Students see it on the course page at once.

The session is opened on the set's current version, so you can't open one
until a version has built. A live session starts with the first exercise
unlocked. An open one has everything unlocked.

A live session can be turned into an open one later with **Switch to open**,
for instance to leave the exercises as homework after class. It can't be
turned back.

## The dashboard {#dashboard}

The dashboard is your view of the class, for your own screen. It updates by
itself every few seconds.

The grid has a row per student and a column per exercise. Each cell is green
when that student has solved the exercise, yellow when they have checked it
but not solved it yet, and empty when they haven't checked it at all. A
spinner means a Check is waiting for the judge. Only **Check** counts: a
student who has only used **Run** shows as not tried. Solved stays solved,
even if a later attempt fails. Under each exercise's number are its counts of
solved and tried, and the columns of locked exercises are dimmed.

Click a cell to see that student's latest Check and latest Run of that
exercise below the grid: the code they sent and the full result, hidden tests
included.

The rows are the session's participants: the students who opened the session,
not everybody on the course. Staff never count as participants.

The dashboard shows names, so don't project it. Use the
[projector](/docs/exercises#projector) for the class.

## The projector {#projector}

The projector is the screen to put up in front of the class. It never shows a
name: only how many students took part and, for each exercise they can see,
how many have solved it, tried it, or not tried it yet, as a bar. It updates by
itself every few seconds. The titles of locked exercises aren't on it either.

It has the controls you need while teaching, so you can drive the session from
the projected screen:

- **← Previous**, **Next →** and **Unlock all**, in a live session (see
  [Unlocking exercises](/docs/exercises#unlocking));
- **Reveal solution**, and once revealed, **Show solution**, which shows it in
  large type below the bars until you press **Hide**;
- **Open editor**, which opens the exercise in the
  [preview](/docs/exercises#preview) in a new tab, so you can write and run a
  solution live. Nothing you run there is recorded in the session.

## Unlocking exercises {#unlocking}

In a live session, students can open exercises 1 up to the last one you have
unlocked, and go back to earlier ones whenever they like. The controls are on
the session's page and on the [projector](/docs/exercises#projector):

- **Next →** unlocks the next exercise.
- **← Previous** locks the last unlocked exercise again.
- **Unlock all** unlocks every exercise, and the session stays live.

Students don't see a locked exercise's statement, or even its title. If they
try to open one, they get a "Wait for the teacher" page, which opens the
exercise by itself, within a few seconds, as soon as you unlock it. The other
students see the new exercise appear in their row of exercise buttons. If you
lock an exercise again while a student has it open, their page stays, but Run
and Check are refused until you unlock it.

Several teachers can drive the same session at once, each from their own
screen. Each press says which exercise the page showed, so if two of you press
**Next →** at the same moment, the class moves on by one exercise, not two.
The session's page doesn't refresh by itself, so a press made from an
out-of-date copy changes nothing: cocco says somebody else may have moved it,
and shows where the session stands now. The projector refreshes itself, so its
buttons stay current.

### Revealing a solution {#reveal}

**Reveal solution**, beside each exercise on the session's page or on the
projector, shows your solution to the class, below the exercise's statement.
It can't be hidden again. You can reveal a solution in a closed session too,
for students looking back at their work.

## Switching version {#switch-version}

When you fix a set after opening a session, the session stays on its old
version. Once the new version is built and current, the session's **Version**
card offers **Switch to v…**. The confirmation lists what will change: titles
that differ, exercises added at the end, and exercises no longer in the file.

Exercises are matched by position: exercise 3 of the old version becomes
exercise 3 of the new one. Check the list of changed titles carefully: if you
moved exercises around in the file, students' work would end up under a
different exercise.

For students, the change is quiet. They get the new statements and tests from
their next click, and the code in their editor stays. Past checks keep their
result, so a solved exercise stays solved, and a result from before the
switch is marked as having run on an earlier version. Nothing is checked
again. A live session keeps the exercises it had unlocked, and an open session
unlocks any added ones too.

The switch is refused when the new version has fewer exercises than the
highest one a student has already checked, and when it is in another
language. In that case, add the exercise back, or open a new session. A closed
session can't be switched.

## Closing a session {#close}

**Close session…** ends the session for good. A closed session can't be
reopened.

From then on, Run and Check are refused, and the session's pages tell students
it is closed. Their work stays: they can open every exercise they
had unlocked and see the code they last ran or checked, with its results. Code
they typed but never ran isn't kept. Checks that were already waiting for the
judge still finish. The session moves under "Closed sessions" on the course
page.

The dashboard and the projector stay available, and you can still reveal
solutions. The pacing and the version are frozen.

Close a session when the lesson is over. If you want students to keep working
at home, use **Switch to open** instead.

## Previewing exercises {#preview}

The preview lets you try a version the way a student would, with nothing
recorded anywhere. Open it with **Preview** in the set's **Versions** list,
from a problem in the **Latest import** card, or with **Open editor** on the
projector.

It is the student's page, with more on it: the hidden tests are shown and
marked "staff only", and the solution is under **Solution · staff only**, with
**Put it in the editor** to load it. **Run** and **Check** go through the
judge exactly as a student's would. The editor starts from the starter code
every time.

Inside a session, Run and Check are the students' only. You can open any of
the session's exercises, even locked ones, but your own attempts belong in
the preview, so they never show up on the dashboard.

## Deleting {#delete}

**Delete session…**, in the session page's **Danger zone**, removes the
session and everything in it: every Run and Check its students made, who took
part, and which solutions were revealed. The set and its other sessions stay.
You can delete a session whatever its state, live included. To confirm, type
its label exactly as shown. There is no undo, so close a session rather than
delete it if students may want to look back at it.

**Delete set…**, in the set page's **Danger zone**, removes every version of
the set and every preview run on them. It is only possible while the set has
no sessions, since those hold the students' work: delete its sessions first.
To confirm, type the set's title exactly as shown.
