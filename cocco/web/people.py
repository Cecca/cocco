"""The course people page: who is enrolled, in what role, and how that changes."""

import logging
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.auth import get_or_create_user, is_allowed_email, normalize_email
from cocco.model import UserRole
from cocco.repository.courses import (
    delete_enrollment,
    lock_course,
    query_course,
    query_course_people,
    query_course_role,
    set_enrollment_role,
)
from cocco.web import core
from cocco.web.access import CourseEditor, CurrentUser
from cocco.web.core import DBDep
from cocco.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


########################################################################
# The course's people: who is enrolled, in what role, and the two ways that
# changes. Course staff only, through `CourseEditor` - and keyed on the course
# for the reason that dependency exists, so staff of one course are strangers
# on every other. A site administrator passes it without an enrollment, which
# is `effective_course_role`'s doing and not this block's.
#
# Declared here, above `get_challenge`, and that is load-bearing exactly as it
# is for the course editor: `/courses/{course_id}/{challenge_id}` matches any
# single segment, so a `/courses/1/people` declared after it would be routed
# there and rejected as an unparseable id before this ever ran.
#
# It is `/admin` reshaped for one course, and it keeps that page's rule: one
# swap target, and every route - the filter box's GET and all three mutations -
# answers with the whole list, alert folded in, so the sentence and the new
# state arrive together instead of being kept in step. Every mutation carries
# `q`, so acting under a filter leaves the filter in force.


async def _render_course_people(
    request: Request,
    db: AsyncConnection,
    course_id: int,
    user: schema.User,
    q: str,
    message: str | None = None,
    ok: bool = False,
):
    """The roster, as every route on the people page answers with it."""
    people = await query_course_people(db, course_id, q=q)
    return templates.TemplateResponse(
        request=request,
        name="f-course-people.jinja",
        context={
            "people": people,
            "course_id": course_id,
            "q": q,
            "user": user,
            "message": message,
            "ok": ok,
        },
    )


@router.get("/courses/{course_id}/people")
async def get_course_people(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
):
    """The class list: everyone enrolled, in what role, and how much they have
    submitted here."""
    course = await query_course(db, course_id)
    if course is None:
        raise HTTPException(status_code=404)
    people = await query_course_people(db, course_id)
    return templates.TemplateResponse(
        request=request,
        name="course-people.jinja",
        context={
            "course": course,
            "course_id": course_id,
            "people": people,
            "q": "",
            "title": f"People on {course.title}",
            "user": user,
            "message": None,
            "ok": False,
        },
    )


@router.get("/f/courses/{course_id}/people")
async def get_fragment_course_people(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    q: str = "",
):
    """The roster, re-rendered as the filter box is typed in. The page inlines
    this same fragment on first paint."""
    return await _render_course_people(request, db, course_id, user, q)


@router.post("/courses/{course_id}/people")
async def post_course_person(
    course_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    email: Annotated[str, Form()] = "",
    new_role: Annotated[str, Form()] = UserRole.student.value,
    q: Annotated[str, Form()] = "",
):
    """Put somebody on the course by address, in whatever role staff chose.

    By address, like `post_administrator`, and for half of its reason: this is
    where a teacher types out a roster, and most of those people have not signed
    in yet. Unlike that table, `enrollment` is keyed on `user.id` and so cannot
    name an address with no account behind it - which is why the account is
    *created* here, through the same `get_or_create_user` a provider's callback
    uses. The row then sits waiting, and their first sign-in finds it.

    That makes this the second place in the app where a human types an address,
    so it is the second that has to normalize one and refuse an address outside
    `allowed_domains` - a pre-enrolled student who could never sign in is a typo
    worth catching at the form, where somebody is standing who can fix it.

    A blocked account is deliberately *not* refused, unlike an appointment: that
    one is one-way through the app, while this is undone by the button in the
    row it creates - and a block stops a person without retracting the fact that
    they are on the course. The row says `blocked` and that is the honest answer.
    """
    try:
        email = normalize_email(email)
    except ValueError as err:
        return await _render_course_people(
            request, db, course_id, user, q, message=str(err)
        )

    if not is_allowed_email(email, core.CONFIG.auth.allowed_domains):
        domains = ", ".join("@" + d for d in core.CONFIG.auth.allowed_domains)
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                f"{email} could never sign in: this server only admits "
                f"an address at {domains}."
            ),
        )

    # Through the enum, never as text - the same rule the leaderboard criterion
    # follows. A role is not something a form field gets to spell.
    try:
        wanted = UserRole(new_role)
    except ValueError:
        return await _render_course_people(
            request, db, course_id, user, q, message="That is not a role."
        )

    # Before the account is created, so a POST naming a course that is not there
    # does not leave a stray user behind. `lock_course` is the existence check
    # and the serialization in one - see it for why every mutation takes it.
    if not await lock_course(db, course_id):
        raise HTTPException(status_code=404)

    person = await get_or_create_user(db, email)
    # Only to word the sentence below; the write is the same either way, and a
    # race here changes an adjective and nothing else.
    before = await query_course_role(db, user_id=person.id, course_id=course_id)

    if not await set_enrollment_role(db, course_id, person.id, wanted):
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                f"{email} is this course's last teacher, so they cannot be "
                "made anything else. Make somebody else a teacher first."
            ),
        )
    # get_db hands out a plain connection with no surrounding transaction
    await db.commit()

    if before is None:
        message = f"{email} is now a {wanted.value} on this course."
    elif before is wanted:
        message = f"{email} was already a {wanted.value} on this course."
    else:
        message = f"{email} is a {wanted.value} now, not a {before.value}."
    return await _render_course_people(
        request, db, course_id, user, q, message=message, ok=True
    )


@router.post("/courses/{course_id}/people/{user_id}/role")
async def post_course_person_role(
    course_id: int,
    user_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    new_role: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
):
    """Change one person's role on the course.

    The same write as the form above - `set_enrollment_role` is one upsert
    behind both, because `enrollment` is keyed on (user_id, course_id) and so
    "enroll them as a teacher" and "make them a teacher" name one row. What is
    refused is the same too: a course must not lose its last teacher, and a
    demotion is one of the two ways that could happen.
    """
    try:
        wanted = UserRole(new_role)
    except ValueError:
        return await _render_course_people(
            request, db, course_id, user, q, message="That is not a role."
        )

    if not await lock_course(db, course_id):
        raise HTTPException(status_code=404)

    if not await set_enrollment_role(db, course_id, user_id, wanted):
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                "Nobody was changed: a course cannot lose its last teacher. "
                "Make somebody else a teacher first."
            ),
        )
    await db.commit()
    return await _render_course_people(
        request,
        db,
        course_id,
        user,
        q,
        message=f"That role is now {wanted.value}.",
        ok=True,
    )


@router.post("/courses/{course_id}/people/{user_id}/remove")
async def post_course_person_remove(
    course_id: int,
    user_id: int,
    request: Request,
    db: DBDep,
    user: CurrentUser,
    role: CourseEditor,
    q: Annotated[str, Form()] = "",
):
    """Take somebody off the course.

    Two refusals, and they sit in different places on purpose. Removing
    *yourself* is checked here, because it is not a race - nobody becomes
    somebody else between two statements - and because it deserves its own
    sentence: it would shut the door on the very page you are standing on.
    Removing the last teacher is checked inside the `DELETE`'s WHERE clause, the
    way `block_user` refuses an administrator, so the rule and the write cannot
    be told apart by a demotion landing between them.

    Nothing of theirs goes with the row: their submissions, outcomes and
    leaderboard entries stay exactly where they are. What they lose is access
    and their place in the standings - see `delete_enrollment`.
    """
    if user_id == user.id:
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                "You cannot remove yourself from a course you are staff on. "
                "Ask another teacher, or a site administrator."
            ),
        )

    if not await lock_course(db, course_id):
        raise HTTPException(status_code=404)

    if not await delete_enrollment(db, course_id, user_id):
        # False for the last teacher and for a row that is not there, and both
        # get this sentence, because in both cases nothing happened.
        return await _render_course_people(
            request,
            db,
            course_id,
            user,
            q,
            message=(
                "Nobody was removed: a course cannot lose its last teacher."
            ),
        )
    await db.commit()
    return await _render_course_people(
        request,
        db,
        course_id,
        user,
        q,
        message="They are off the course. Their submissions are untouched.",
        ok=True,
    )
