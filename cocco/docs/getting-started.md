---
title: Getting started
audience: staff
order: 10
---

## Courses, challenges and exercises {#overview}

Everything in cocco happens inside a **course**. A course has a title, a short
description, and a list of people, each with a role on it. Anyone signed in
can see that a course exists, but only the people enrolled on it see what is
inside.

A course holds two kinds of work:

- **Challenges** are programming problems with a statement and a set of test
  cases. Students hand in a source file whenever they like, the judge runs it
  in a sandbox, and their best score counts. Each challenge has its own
  leaderboard, and you see the standings of the whole class. See
  [Creating a challenge](/docs/challenges#create).
- **Exercise sets** are short programming exercises for class time, in
  Python, C or C++. You open a **session** of a set for a group, unlock the
  exercises one by one as the lesson goes on, and follow the class's attempts
  live. See [Exercises and challenges](/docs/exercises#what).

The home page lists the courses you are on under **Your courses**, and every
other course under **Other courses**.

## Roles {#roles}

A role belongs to one course: the same person can teach one course and be a
student on another.

- **Teacher**: runs the course. Teachers edit the course and its challenges,
  see the class standings and every student's submissions, manage exercise
  sets and sessions, and decide who is on the course from the **People** page.
  A course always has at least one teacher.
- **Collaborator**: can do everything a teacher can on that course, the People
  page included. The one difference is that collaborators don't count towards
  the teacher a course must keep, so a course can't be left with collaborators
  only. It suits teaching assistants.
- **Student**: reads the published challenges, submits solutions, and sees
  their own results and each challenge's leaderboard. Students take part in
  the exercise sessions you open, and see nothing of what you are still
  preparing.

**Administrators** are not a course role. An administrator is appointed for
the whole site and counts as a teacher on every course, without being
enrolled. Only an administrator can create a course, archive or delete one,
and use the **Administration** page to appoint other administrators and block
accounts.

## Your first course {#first-course}

1. **Create the course.** Only an administrator can do this, with **New
   course** on the home page. Whoever creates it becomes its teacher. If you
   are not an administrator, ask one to create the course and add you as a
   teacher on its People page.
2. **Describe it.** In the course editor (**Edit course** on the course page),
   write the description students read at the top of the course page. If only
   your class should get in, set an
   [enrollment key](/docs/courses#enrollment-key).
3. **Add your colleagues.** On the **People** page, enroll them by address as
   teachers or collaborators. They don't need to have signed in before.
4. **Write a challenge.** **New challenge** opens the form, and **Create
   challenge** takes you to its editor, where you add
   [test cases](/docs/test-cases#add). A new challenge is hidden from students.
   Try it with **View as student**, submitting a reference solution, then make
   it visible (see [Visibility](/docs/challenges#visibility)).
5. **Bring the students in.** They can join themselves with **Enroll!** (with
   the key, if you set one), or you can
   [enroll them by address](/docs/courses#enroll-by-address).
6. **Follow along.** Students submit from the challenge page, and their
   results appear as soon as the judge has run the program. You follow the
   whole class in the [Class standings](/docs/challenges#standings).

For a lesson with exercises, [import an exercise set](/docs/exercises#import)
from the course page and [open a session](/docs/exercises#open-session) for
your group when class begins.
