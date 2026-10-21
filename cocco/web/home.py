"""The home page: the courses list and its filter fragment."""

from fastapi import APIRouter, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.repository.courses import query_courses
from cocco.repository.users import is_administrator
from cocco.web.access import CurrentUser
from cocco.web.core import DBDep
from cocco.web.templating import templates

router = APIRouter()


@router.get("/")
async def index(request: Request, db: DBDep, user: CurrentUser, q: str = ""):
    # Two affordances off one answer: "New course", and the Edit button on the
    # card of a course an administrator is not enrolled on. The routes behind
    # both check for themselves - see `require_admin` and
    # `effective_course_role`. It is also the third thing here: an archived
    # course is not listed to somebody who is not on it, and an administrator
    # is the only person who can unarchive one, so they have to be able to find
    # it.
    is_admin = await is_administrator(db, user.email)
    courses = await query_courses(db, user_id=user.id, q=q, include_archived=is_admin)
    return templates.TemplateResponse(
        request=request,
        name="index-logged-in.jinja",
        context={
            "courses": courses,
            "q": q,
            "title": "COCCO: Coding Challenges Companion",
            "user": user,
            "is_admin": is_admin,
        },
    )


async def _render_courses(
    request: Request, db: AsyncConnection, user: schema.User, q: str
):
    """Both halves of the courses list. Shared by the filter box's GET and by
    `post_course_enroll`, so the list a click leaves behind is the one the box
    would have produced."""
    # `_course-card.jinja` reads this: an administrator is staff on every
    # course, so offering them `Enroll!` on one would be odd. `query_courses`
    # still splits the two halves on enrollment alone - they really are not
    # enrolled, and the split should keep saying so. It also decides whether
    # the archived courses they are not on are listed at all; see `index`.
    is_admin = await is_administrator(db, user.email)
    courses = await query_courses(db, user_id=user.id, q=q, include_archived=is_admin)
    return templates.TemplateResponse(
        request=request,
        name="f-courses.jinja",
        context={
            "courses": courses,
            "q": q,
            "user": user,
            "is_admin": is_admin,
        },
    )


@router.get("/f/courses")
async def get_fragment_courses(request: Request, db: DBDep, user: CurrentUser, q: str = ""):
    """Both halves of the courses list, re-rendered as the filter box is typed
    in. The page inlines this same fragment on first paint, so the filtered
    list and the initial one cannot drift apart.

    No authorization beyond `CurrentUser`: this is exactly what `index` shows,
    and every course is visible to every signed-in user."""
    return await _render_courses(request, db, user, q)
