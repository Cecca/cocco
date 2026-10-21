"""Courses: creating one, the course page, enrolling, the editor and the danger zone."""

import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.auth import safe_next_path
from cocco.model import UserRole
from cocco.repository.courses import (
    course_exists,
    insert_course,
    insert_enrollment,
    insert_self_enrollment,
    query_course,
    query_course_challenges,
    query_course_info,
    query_enrollment_key,
    query_ordered_challenge_ids,
    reorder_challenges,
    update_course,
)
from cocco.repository.exercise_sessions import query_exercise_sessions
from cocco.repository.exercise_sets import query_exercise_sets
from cocco.repository.lifecycle import (
    delete_course,
    lock_course_exercises,
    lock_course_submissions,
    query_challenge_file_paths,
    query_course_archived_at,
    set_course_archived,
)
from cocco.repository.users import is_administrator
from cocco.web import core
from cocco.web.access import (
    Admin,
    ChallengeEditor,
    CourseEditor,
    CurrentUser,
    effective_course_role,
)
from cocco.web.core import EDITOR_ROLES, DBDep
from cocco.web.home import _render_courses
from cocco.web.templating import templates
from cocco.web.testcases import _evaluator_dir, _testcase_dir

logger = logging.getLogger(__name__)

router = APIRouter()


########################################################################
# Creating a course. Administrators only, through `Admin`.
#
# Declared here, above `get_course`, and that is load-bearing for the same
# reason the course editor is declared above `get_challenge`: `/courses/{course_id}`
# matches any single segment, so a `/courses/new` declared after it would be
# routed there and rejected as an unparseable id before this ever ran.


@router.get("/courses/new")
async def get_course_new(request: Request, user: Admin):
    """The empty course form."""
    return templates.TemplateResponse(
        request=request,
        name="course-new.jinja",
        context={"user": user, "title": "New course"},
    )


@router.post("/courses/new")
async def post_course_new(
    request: Request,
    db: DBDep,
    user: Admin,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
):
    """Create the course, enroll its creator, and send the browser to the editor."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    title = title.strip()
    if not title:
        return alert("A course needs a title.")

    course_id = await insert_course(db, title=title, description=description)
    # Being an admin somewhere does not make one staff here: without this row
    # the creator could not open the editor of the course they just made.
    await insert_enrollment(db, user.id, course_id, UserRole.teacher)
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    # A 204 with HX-Redirect rather than a 303, for the reason spelled out in
    # `post_challenge_new`: htmx would splice a whole page into the alert div
    # this form targets. The editor is the right landing place - the new course
    # has no challenges yet, and that is where they are added.
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(
                request.url_for("get_course_edit", course_id=course_id)
            )
        },
    )


async def _render_enroll(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    next: str = "",
    error: str | None = None,
):
    """The way into a course, as the preview shows it: the `Enroll!` button, and
    the key field when the course wants one.

    Shared by `get_course`, which inlines it on first paint, and by
    `post_course_enroll`, which swaps it back with a message when the key was
    wrong - so what a refused student is looking at is the same form they typed
    into. It is handed `requires_key` and never the key itself.
    """
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="f-enroll.jinja",
        context={"course": course, "next": next, "error": error},
    )


@router.get("/courses/{course_id}")
async def get_course(course_id: int, request: Request, db: DBDep, user: CurrentUser):
    """The course, or - for someone not enrolled on it - a preview of it.

    The one page under `/courses/` with no membership gate, and deliberately:
    the home page lists every course, so this is where a link from it lands, and
    it is where the `Enroll!` button makes the most sense. A non-member sees the
    title and the description, which the home page's card already shows
    truncated, and nothing else. The challenge list is not merely hidden in the
    template - `query_course_info` is never run for them.

    "Non-member" is `effective_course_role`'s answer, so a site administrator
    gets the course itself rather than an invitation to join it.
    """
    role = await effective_course_role(db, user, course_id)
    if role is None:
        course = await query_course(db, course_id)
        if course is None:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="course.jinja",
            context={
                "course": course,
                "course_id": course_id,
                "title": course.title,
                "user": user,
                "can_edit": False,
                "is_member": False,
            },
        )

    # The viewer is part of the query twice over: each challenge card says
    # whether *this* person has already submitted to it, and staff are shown
    # the challenges they have not published yet while the class is not. This
    # is the only page where the flag is a decision rather than a constant -
    # `get_course_edit` and the reorder fragment are staff-only by dependency.
    course = await query_course_info(
        db, course_id, user_id=user.id, include_hidden=role in EDITOR_ROLES
    )
    if course is None:
        raise HTTPException(status_code=404)
    can_edit = role in EDITOR_ROLES
    return templates.TemplateResponse(
        request=request,
        name="course.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "title": course["title"],
            "user": user,
            # Only staff are offered the Edit and New challenge buttons. The
            # routes behind them check for themselves - see
            # `require_course_editor`.
            "can_edit": can_edit,
            "is_member": True,
            # Staff only, and not fetched otherwise: a set is content being
            # authored, and the class meets it through a session.
            "exercise_sets": await query_exercise_sets(db, course_id) if can_edit else [],
            # Every member's: a session is how the class meets a set. The
            # closed ones are listed too, apart, because a closed session is
            # still where a student reviews what they did in it.
            "exercise_sessions": await query_exercise_sessions(db, course_id=course_id),
        },
    )


@router.post("/courses/{course_id}/enroll")
async def post_course_enroll(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    q: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
    key: Annotated[str, Form()] = "",
):
    """Enroll the viewer on a course, from either `Enroll!` button.

    Authorization is `CurrentUser` and deliberately nothing else: this is the
    way in, and a membership gate here would lock the door from the inside. It
    is the only route under a course that a non-member may call, which is why
    the enrollment it writes has to be the one thing it can write - the role is
    not a parameter, see `insert_self_enrollment`, which fixes it at `student`
    and leaves an existing enrollment alone, so this can neither promote anyone
    nor demote a teacher who clicks it. Who may sign in at all is
    `is_allowed_email`; who may sign up for *this* course is, for now, anyone
    who can.

    A course with an enrollment key set admits nobody who cannot quote it. The
    field is only on the preview - the home page's card links there instead of
    offering a button - so a wrong key is answered with that same fragment and
    a message, rather than with the courses list: 200 and not a 4xx, because
    htmx does not swap the body of an error response and the fragment is the
    entire point of the request.

    Otherwise two callers, so two answers. From the home page it renders the
    whole list, both halves, because the point of the click is that the course
    moves from "Other courses" up into "Your courses"; `q` comes back with it
    so enrolling under a typed filter does not silently reset the list. From a
    course preview there is no list to re-render - the page has to become the
    course page - so that form sends `next` and gets the `HX-Redirect` idiom
    `post_course_new` uses. `next` is filtered through `safe_next_path` like
    every other destination in the app: it arrives in a request, so it is not
    trusted to be a path on this site.
    """
    if not await course_exists(db, course_id):
        raise HTTPException(status_code=404)

    # Before the key, because an archived course refuses whether or not the key
    # is right - it is not taking students at all. The same fragment the key
    # refusal answers with, for the same reason: what a refused student is
    # looking at is the panel they clicked in. In practice this is only ever
    # reached from a stale tab or a typed URL, since `query_courses` does not
    # offer an archived course to anybody who is not on it - which is exactly
    # why the route has to say it rather than the listing.
    if await query_course_archived_at(db, course_id) is not None:
        return await _render_enroll(
            request,
            db,
            course_id,
            next,
            error="This course is archived and is not taking new students.",
        )

    expected = await query_enrollment_key(db, course_id)
    if expected is not None and not hmac.compare_digest(key.strip(), expected):
        # Trimmed, because a key read off a slide is pasted with whitespace,
        # but otherwise exact - it is a string a teacher chose, and deciding
        # that case does not count is not ours to make. `compare_digest`
        # rather than `==` so the comparison does not time out early.
        return await _render_enroll(
            request, db, course_id, next, error="That key is not right."
        )

    await insert_self_enrollment(db, user_id=user.id, course_id=course_id)
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    if next:
        return Response(
            status_code=204, headers={"HX-Redirect": safe_next_path(next)}
        )
    return await _render_courses(request, db, user, q)


########################################################################
# The course editor: the course's own description, and the order its challenges
# are listed in. Course staff only, through `CourseEditor`.
#
# Declared here, above `get_challenge`, and that is load-bearing: its path is
# `/courses/{course_id}/{challenge_id}`, whose `{challenge_id}` matches any
# single segment, so a `/courses/1/edit` declared after it would be routed
# there and rejected as an unparseable id before this ever ran.


async def _render_course_challenges(
    request: Request, db: DBDep, course_id: int, user: schema.User
):
    """The reorderable challenge list. Every move returns this whole fragment,
    so the cards always sit in the order the database has them.

    `include_hidden`, unconditionally: this is the course editor's copy of the
    list and every caller of it is behind `CourseEditor`. Hiding a challenge
    from the page you publish it on would be a fine joke.
    """
    challenges = await query_course_challenges(
        db, course_id, user_id=user.id, include_hidden=True
    )
    return templates.TemplateResponse(
        request=request,
        name="f-course-challenges.jinja",
        context={
            "challenges": challenges,
            "course_id": course_id,
            "user": user,
        },
    )


@router.get("/courses/{course_id}/edit")
async def get_course_edit(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
):
    course = await query_course_info(
        db, course_id, user_id=user.id, include_hidden=True
    )
    if course is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="course-edit.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "title": f"Editing {course['title']}",
            "user": user,
            # The danger zone at the foot of this page is administrators only,
            # and `require_course_editor` lets a plain teacher in here - so the
            # card has to ask its own question. The routes behind it are behind
            # `Admin`; this only decides whether it is drawn.
            "is_admin": await is_administrator(db, user.email),
        },
    )


@router.post("/courses/{course_id}/edit")
async def post_course_edit(
    course_id: int,
    request: Request,
    db: DBDep,
    role: CourseEditor,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
    enrollment_key: Annotated[str, Form()] = "",
):
    """Save the course's title, description and enrollment key."""

    def alert(message: str, ok: bool = False):
        return templates.TemplateResponse(
            request=request,
            name="f-edit-result.jinja",
            context={"ok": ok, "message": message},
        )

    title = title.strip()
    if not title:
        return alert("A course needs a title.")

    # An empty box is NULL, not the empty string: NULL is what "anyone may
    # join" is spelled as, and clearing the field has to reopen the course
    # rather than set a key nobody can type.
    key = enrollment_key.strip() or None
    await update_course(
        db,
        course_id,
        title=title,
        description=description,
        enrollment_key=key,
    )
    await db.commit()
    return alert(
        "Saved. Students need the enrollment key to join." if key else "Saved.",
        ok=True,
    )


@router.post("/courses/{course_id}/challenges/{challenge_id}/move")
async def post_challenge_move(
    course_id: int,
    challenge_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: ChallengeEditor,
    direction: Annotated[str, Form()],
):
    """Move a challenge one place up or down the course page. Arrows rather
    than drag-and-drop, exactly as `post_testcase_move`: no JS build step, and
    this is a list of integers.

    Unlike that one it hands the whole reordered course to the repository
    rather than naming two rows to exchange - see `reorder_challenges` for why
    a swap is not enough here.
    """
    if direction not in ("up", "down"):
        raise HTTPException(status_code=404)

    ordered = await query_ordered_challenge_ids(db, course_id)
    index = next((i for i, cid in enumerate(ordered) if cid == challenge_id), None)
    if index is None:
        raise HTTPException(status_code=404)

    neighbour = index - 1 if direction == "up" else index + 1
    if not 0 <= neighbour < len(ordered):
        # Already at the end; nothing to do rather than an error
        return await _render_course_challenges(request, db, course_id, user)

    ordered[index], ordered[neighbour] = ordered[neighbour], ordered[index]
    await reorder_challenges(db, ordered)
    await db.commit()
    return await _render_course_challenges(request, db, course_id, user)


########################################################################
# The course's danger zone: archiving it, and ending it. Site administrators
# only, through `Admin`, and deliberately *not* `CourseEditor` - which
# `get_course_edit` lets a plain teacher through. What the home page lists, and
# what exists at all, is the site's business rather than one course's; it is
# the same line `post_course_new` draws at the other end of a course's life,
# and the reason `require_admin`'s sentence names the permission rather than
# either of the places it is used - this is now the third.
#
# Declared here, above `get_challenge`, for the reason the editor above is:
# `/courses/{course_id}/{challenge_id}` matches any single segment. These are
# POSTs and would survive being declared below it today - a path match with the
# wrong method is only a partial match, and the router keeps looking - but that
# is an accident of the method rather than a rule, and the next GET added here
# would find out the hard way.
#
# Archive and unarchive are two routes rather than one with a flag, mirroring
# `/admin/users/{id}/block` and `/unblock`: the closest analogue in the app, a
# reversible state an administrator toggles, with each URL meaning one thing.
# Both answer with `HX-Redirect` back to the same editor rather than a fragment
# swap, because archiving changes the header badge, every challenge card's
# greying and the button's own label - the page *is* the one swap target.


def _danger_alert(request: Request, message: str, ok: bool = False):
    """The alert both danger zones swap in. The same fragment every other
    editor route answers with; it is the sentence that differs."""
    return templates.TemplateResponse(
        request=request,
        name="f-edit-result.jinja",
        context={"ok": ok, "message": message},
    )


def _judge_busy_message(running: int, what: str) -> str:
    """`post_regrade`'s refusal, worded for a delete. Spelled once because both
    danger zones refuse in exactly the same circumstances."""
    return (
        f"A judge is running {running} submission{'' if running == 1 else 's'} "
        f"on this {what} right now. Nothing was deleted — try again in a moment."
    )


def _confirmation_matches(typed: str, title: str) -> bool:
    """Whether the name typed into a delete dialog is the thing's own.

    Checked on the server and not only in the dialog: the script there merely
    *enables* the button, and a disabled control is presentation, never the
    check - the rule `require_challenge_editor` states for hidden buttons.

    A plain `==` and deliberately not `hmac.compare_digest`, unlike the
    enrollment key: the title is printed on the page the box sits on, so this
    confirms an intention rather than proving a secret.
    """
    return typed.strip() == title.strip()


@router.post("/courses/{course_id}/archive")
async def post_course_archive(course_id: int, request: Request, db: DBDep, user: Admin):
    """Archive a course: read-only for the people already on it, invisible and
    unjoinable to everybody else. Staff go on editing it - archiving is a
    visibility state, not a lock, which is what keeps the whole feature down to
    three enforcement points (`post_submission`, `post_course_enroll`, and the
    routes that queue an exercise Run or Check)."""
    if not await course_exists(db, course_id):
        raise HTTPException(status_code=404)
    await set_course_archived(db, course_id, True)
    await db.commit()
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(request.url_for("get_course_edit", course_id=course_id))
        },
    )


@router.post("/courses/{course_id}/unarchive")
async def post_course_unarchive(
    course_id: int, request: Request, db: DBDep, user: Admin
):
    """Undo the above. Clears the column rather than writing a second one - an
    archive that is over has no date."""
    if not await course_exists(db, course_id):
        raise HTTPException(status_code=404)
    await set_course_archived(db, course_id, False)
    await db.commit()
    return Response(
        status_code=204,
        headers={
            "HX-Redirect": str(request.url_for("get_course_edit", course_id=course_id))
        },
    )


@router.post("/courses/{course_id}/delete")
async def post_course_delete(
    course_id: int,
    request: Request,
    db: DBDep,
    user: Admin,
    confirm: Annotated[str, Form()] = "",
):
    """Delete a course, every challenge on it, every submission ever made to
    them and every file they own.

    `confirm` is the course's own title, typed into the dialog. It is a
    **defaulted** form field for `post_administrator`'s reason: a missing field
    is a 422, htmx drops the body of an error response, and the person who
    clicked would be left looking at nothing.

    Refusals are 200 with an alert, the success is a 204 carrying
    `HX-Redirect` - there is no page left to swap a fragment into.
    """
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)

    if not _confirmation_matches(confirm, course.title):
        return _danger_alert(
            request,
            "Type the course's title exactly as it is shown to delete it. "
            "Nothing was changed.",
        )

    # Refused while a judge is mid-run, as a regrade is - and for a sharper
    # reason. See `lock_challenge_submissions`: a submission deleted mid-run
    # takes the judge *process* down, where the same race against a regrade
    # only leaves one stale score. The lock is what makes this more than
    # usually true.
    running = await lock_course_submissions(db, course_id)
    if running:
        return _danger_alert(request, _judge_busy_message(running, "course"))
    # The exercises need no such refusal - a job the judge finishes on a
    # deleted session updates zero rows - only the locks that keep a new job,
    # preview or version from landing among the deletes.
    await lock_course_exercises(db, course_id)

    # Read before the deletes, unlinked after the commit: the order
    # `post_testcase_delete` states. A failed commit can leave a file nothing
    # points at, but never a row pointing at a file that is gone.
    #
    # `_testcase_dir` and `_evaluator_dir` are defined in `web/testcases.py`,
    # with the test case and evaluator routes they belong to. Importing them
    # here does not move this route: its place is set by `include_router`
    # order in `main.py`, which must put `courses.router` above `challenges`.
    challenge_ids = await query_ordered_challenge_ids(db, course_id)
    paths: list[str] = []
    for challenge_id in challenge_ids:
        paths.extend(await query_challenge_file_paths(db, challenge_id))

    await delete_course(db, course_id)
    await db.commit()

    for path in paths:
        core.storage.delete(path)
    for challenge_id in challenge_ids:
        core.storage.delete_directory(_testcase_dir(challenge_id))
        core.storage.delete_directory(_evaluator_dir(challenge_id))

    return Response(
        status_code=204, headers={"HX-Redirect": str(request.url_for("index"))}
    )
