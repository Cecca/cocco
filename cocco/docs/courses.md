---
title: Courses and people
audience: staff
order: 15
---

A course holds challenges and exercise sets, and the people who work on them.
Its staff edit it from the course editor (**Edit course** on the course page)
and manage who is on it from the **People** page.

## Creating a course {#create}

Only a site administrator can create a course. They see a **New course**
button on the home page; nobody else does. If you need a course and are not an
administrator, ask one to create it and to enroll you as a teacher.

The form asks for a **Title** and a **Description**. **Create course** takes
you straight to the course editor, where you set an
[enrollment key](/docs/courses#enrollment-key) if you want one and add the
challenges. Whoever creates the course is enrolled on it as its teacher.

A new course is open at once: anyone signed in finds it under **Other
courses** on their home page, can read its title and description, and can
join it with **Enroll!** unless you set a key. It has no challenges yet, so
there is nothing for them to do until you publish one.

## Title {#title}

The title is how the course is listed on everyone's home page, and it heads
the course page. You can change it at any time in the course editor and press
**Save changes**. The new title shows everywhere straight away.

## Enrollment key {#enrollment-key}

The key decides who can join the course by themselves.

- **No key** (the field empty): anyone signed in can join. The course's card
  on their home page has an **Enroll!** button, and so does the course page.
- **A key**: students have to type it before they are enrolled. Their home
  page shows **Enroll…** instead, which leads to the course page with a field
  for the key and the line "This course needs an enrollment key. Ask the
  course staff for it." A wrong key gets "That key is not right."

The key is compared exactly, capitals included. Spaces before or after it are
ignored, so a key pasted from a slide still works. It is shown in the clear in
the course editor, so you can read it out or put it on a slide. Students never
see it anywhere in cocco.

The key only guards the way in. Changing or clearing it doesn't affect anyone
already on the course, and people you
[enroll by address](/docs/courses#enroll-by-address) don't need it. Whoever
joins by themselves always joins as a student.

The key can be set only in the course editor, not on the New course form.

## Description {#description}

The description is the introduction at the top of the course page: what the
course is about, how it is graded, where to ask for help. It is written in
Markdown, with the same code blocks and tables as a challenge statement (see
[Problem statement](/docs/challenges#statement)), and the preview beside the
text shows it the way the course page will.

People who are not on the course see it too. Before they join, the course page
shows them only the title, the description and the way to enroll, so the
description is also where you tell them whether this course is theirs. The
course's card on the home page shows the first couple of lines of the text as
you typed it, without the Markdown formatting.

## Challenge order {#challenge-order}

The **Challenges** card in the course editor lists every challenge in the
order students see them, hidden ones included. Move a challenge with the ↑
and ↓ beside it. Only the listing changes: no submission, score or test case
is touched.

The "Challenge 01", "Challenge 02" numbers follow this order and are
renumbered after every move. Hidden challenges are counted too, so the numbers
students see match yours. A student may see a gap, Challenge 01 then
Challenge 03, while Challenge 02 is still hidden.

**New challenge** in this card, or on the course page, starts a new one at the
end of the list (see [Creating a challenge](/docs/challenges#create)).

## Archiving a course {#archive}

Archiving puts a finished course away without losing anything. Only site
administrators can do it: the course editor's **Danger zone** is shown to
them alone.

Once the course is archived:

- students already on it keep reading everything (statements, their own
  attempts, scores and leaderboards) but can no longer submit to any
  challenge, and the exercises in its sessions can no longer be run or
  checked;
- it disappears from **Other courses** for everyone who isn't on it, and
  nobody can join it with **Enroll!**;
- it is marked "Archived" on the course page and on the home page of the
  people who are on it.

Staff can still edit the course and its challenges. **Unarchive**, in the same
place, reopens it. Each challenge then goes back to whatever it was before,
so a challenge you archived on its own stays archived (see
[Archiving a challenge](/docs/challenges#archive)).

## Deleting a course {#delete}

**Delete course…**, in the course editor's **Danger zone**, removes the course
for good: every challenge with its test cases and evaluator, every submission
and score, every exercise set and session, and every enrollment. There is no
undo. Only site administrators see this button. If you only want the course
out of the way, archive it instead.

To confirm, type the course's title exactly as shown and press **Delete for
good**. You then land on the home page.

If the judge is running a submission from this course at that moment, the
delete is refused and nothing changes. Try again a moment later.

## People {#people}

The **People** page lists everyone enrolled on the course: their role, how
many submissions they have made on this course, and when they last submitted.
Staff are listed first. Only the course's staff and site administrators can
open it.

Type in the filter box to find someone by name or address. From this page you
can [enroll someone by address](/docs/courses#enroll-by-address),
[change a role](/docs/courses#roles), or take someone off the course with
**Remove**.

Removing someone takes away their access to the course, but keeps their work.
Their submissions and scores stay, they still appear in the
[Class standings](/docs/challenges#standings), and their best result stays on
the leaderboard. If they have submitted anything, cocco asks you to confirm
first. If the course has no enrollment key they can join again by themselves,
as a student.

You can't remove yourself. Ask another teacher, or a site administrator. A
student can't leave a course by themselves either.

A person whose account a site administrator has blocked shows a "blocked"
badge. The block keeps them out of cocco, but they stay on the course.

## Enrolling by address {#enroll-by-address}

Under **Enrol somebody**, type an address, choose a role and press **Enrol**.
This is how you add colleagues as teachers or collaborators, and how you put a
class list on the course before the lesson.

The person doesn't need to have signed in before. If cocco has never seen the
address, it creates an account for it, and the enrollment is there the first
time they sign in with that address. The address must belong to a domain this
server accepts for signing in; any other address is refused, since that person
could never sign in.

You don't need the [enrollment key](/docs/courses#enrollment-key) to enroll
someone. If the person is already on the course, the form changes their role
to the one you chose.

## Changing a role {#roles}

Each person's role is the drop-down in the **Role** column. Pick a new one and
it is saved at once, with no button to press. The change applies to this
course only.

- **teacher** and **collaborator** are course staff: they edit the course and
  its challenges, see every student's results and manage the people here.
- **student** reads the published challenges and submits to them.

Making a student a collaborator is how you give a teaching assistant access to
the standings and the editors. Making yourself a student takes away your own
staff access to the course. See [Roles](/docs/getting-started#roles) for
everything each role can do.

A course can't be left without a teacher (see
[The last teacher](/docs/courses#last-teacher)).

## The last teacher {#last-teacher}

Every course keeps at least one teacher, so there is always someone who can
manage it. On the People page, the only teacher is marked "the last one" and
has neither a role drop-down nor a **Remove** button. Collaborators don't
count: a course with one teacher and several collaborators still has a last
teacher.

To hand the course over, make someone else a teacher first. Then the former
teacher can be made a collaborator or a student, or removed.
