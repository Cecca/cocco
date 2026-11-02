---
title: Administration
audience: staff
order: 45
---

Site administrators look after the whole server rather than one course. This
page is for them; other staff see none of these controls.

## Administrators {#administrators}

An administrator can create courses, archive and delete them, and use the
**Administration** page, reached from the button of that name on the home
page. They also count as a teacher on every course without being enrolled on
it, so they can open, edit and manage the people of any course.

The Administration page lists every account on the server, including people
enrolled by address who haven't signed in yet, with how many courses they are
on and how many submissions they have made. Type in the
filter box to find someone by name or address. Click a name to open that
person's profile. You see their details and courses, but not their API tokens
or password, and you can't change anything there.

To appoint another administrator, type their address under **Appoint an
administrator** and press **Appoint**. They don't need to have signed in
before: until they do, they are listed under "Appointed, never signed in". The
address must be one this server accepts for signing in.

Appointing can't be undone from the web: there is no button to remove an
administrator. Whoever runs the server can do it in the database. The server's
configuration file can also name administrators, who are appointed every time
the server starts.

## Blocking an account {#blocking}

Blocking shuts a person out of the whole server. Type a reason in the box on
their row, if you want one, and press **Block**. The reason is shown to them.

From then on, whatever page they open shows "You are shut out" with your
reason, and signing in again is refused with the same sentence. The command
line refuses their API tokens too.

A block takes nothing away. Their submissions still count, they stay on the
standings and leaderboards, and they stay on their courses, marked "blocked"
on the People page. Submissions they made before the block are still graded.

**Unblock**, on the same row, lets them back in as before.

An administrator can't be blocked, and a blocked address can't be appointed
administrator. Unblock it first.

### Resetting a password {#reset-password}

When the server allows signing in with a password, each row also has **Reset
password**. It makes up a new password and shows it to you once, at the top
of the list. Pass it on by some other means than this page, and ask the person
to set their own from their profile (see [Password](/docs/account#password)).
Until they do, you know their password too.

## Creating courses {#creating-courses}

Only administrators can create courses, with **New course** on the home page.
Whoever creates a course is enrolled on it as its teacher. To set a course up
for a colleague, create it and enroll them as a teacher on its **People** page
(see [Enrolling by address](/docs/courses#enroll-by-address)). You can't
remove yourself, but once they are on it they can remove you. As an
administrator you can still reach the course afterwards.

See [Creating a course](/docs/courses#create) for the form and what a new
course looks like to students.

## The course danger zone {#course-danger-zone}

The **Danger zone** at the bottom of the course editor is shown to
administrators only. It holds two actions:

- **Archive**, which closes a finished course but keeps it readable. See
  [Archiving a course](/docs/courses#archive).
- **Delete course…**, which removes the course and everything in it for good.
  See [Deleting a course](/docs/courses#delete).

Course teachers can archive or delete their own challenges, exercise sets and
sessions, but not the course itself. Ask them before you archive or delete one
of their courses.
