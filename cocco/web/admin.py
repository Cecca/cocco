"""Administering the site: the user list, administrators, blocks and password resets."""

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.concurrency import run_in_threadpool

from cocco import schema
from cocco.auth import is_allowed_email, normalize_email
from cocco.passwords import generate_password, hash_password
from cocco.repository.credentials import set_user_password
from cocco.repository.users import (
    block_user,
    insert_administrators,
    query_user,
    query_user_by_email,
    query_users,
    unblock_user,
)
from cocco.web import core
from cocco.web.access import Admin
from cocco.web.core import DBDep
from cocco.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


########################################################################
# Administering the site. Administrators only, through `Admin` - the second
# thing that dependency guards, and the reason its sentence names the
# permission rather than creating a course.
#
# Unlike its neighbour above, the position of this block is not load-bearing:
# `/admin` is a literal segment that collides with no path parameter.
#
# Every mutation here answers with the user list, alert included, rather than
# with an alert alone: it is `post_course_enroll` answering with the courses
# list it changed, with `f-enroll.jinja`'s rule folded in - what you are
# looking at after a refusal is the thing you acted on. One swap target means
# the sentence and the new state arrive together instead of being kept in step.


async def _render_users(
    request: Request,
    db: AsyncConnection,
    user: schema.User,
    q: str,
    message: str | None = None,
    ok: bool = False,
    issued: tuple[str, str] | None = None,
):
    """The user list, as every route on the admin page answers with it."""
    users = await query_users(db, q=q)
    return templates.TemplateResponse(
        request=request,
        name="f-users.jinja",
        context={
            "users": users,
            "q": q,
            "user": user,
            "message": message,
            "ok": ok,
            # Draws the per-row reset button. Off unless the deployment has
            # passwords at all, in which case there is nothing to reset.
            "password_login": core.CONFIG.auth.password_login,
            # The one thing on this page no query can produce: a freshly issued
            # password, on the single response that made it. `f-tokens.jinja`
            # shows a minted token the same way and for the same reason - only
            # its hash is stored, so there is nowhere to read it back from.
            "issued": issued,
        },
    )


@router.get("/admin")
async def get_admin(request: Request, db: DBDep, user: Admin, q: str = ""):
    """The admin page: everyone the site knows about, and the way to appoint
    another administrator."""
    users = await query_users(db, q=q)
    return templates.TemplateResponse(
        request=request,
        name="admin.jinja",
        context={
            "users": users,
            "q": q,
            "title": "Administration",
            "user": user,
            "message": None,
            "ok": False,
            "password_login": core.CONFIG.auth.password_login,
            "issued": None,
        },
    )


@router.get("/f/users")
async def get_fragment_users(request: Request, db: DBDep, user: Admin, q: str = ""):
    """The user list, re-rendered as the filter box is typed in. The page
    inlines this same fragment on first paint."""
    return await _render_users(request, db, user, q)


@router.post("/admin/administrators")
async def post_administrator(
    request: Request,
    db: DBDep,
    user: Admin,
    # Defaulted rather than required, so an empty box becomes the alert below
    # instead of a 422 - which htmx would drop on the floor, leaving the person
    # who submitted it looking at nothing at all.
    email: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
):
    """Appoint a site administrator, by address.

    By address and not by user id, because that is what `administrator` holds:
    the point of the table is that it can name someone before they have ever
    signed in, and this form is the ordinary way that happens - a colleague is
    appointed, and their account appears on their first sign-in.

    This is the only place in the app where a human types an address, so it is
    the only place that has to normalize one. It also refuses an address outside
    `allowed_domains`, which `_load_auth_config` merely warns about: a bad line
    in `cocco.toml` must not stop the app from booting, but a form has somebody
    in front of it who can fix the typo.
    """
    try:
        email = normalize_email(email)
    except ValueError as err:
        return await _render_users(request, db, user, q, message=str(err))

    if not is_allowed_email(email, core.CONFIG.auth.allowed_domains):
        domains = ", ".join("@" + d for d in core.CONFIG.auth.allowed_domains)
        return await _render_users(
            request,
            db,
            user,
            q,
            message=(
                f"{email} could never sign in: administrators must have "
                f"an address at {domains}."
            ),
        )

    row = await query_user_by_email(db, email)
    if row is not None and row.blocked_at is not None:
        # The other half of "an administrator is never blocked". Appointing is
        # one-way through the app, so without this an administrator could be
        # made who cannot sign in and cannot be unmade.
        return await _render_users(
            request,
            db,
            user,
            q,
            message=f"{email} is blocked. Unblock them first.",
        )

    # ON CONFLICT DO NOTHING, so appointing someone twice is a no-op rather
    # than an error - and the message below is true either way.
    await insert_administrators(db, [email])
    await db.commit()
    return await _render_users(
        request, db, user, q, message=f"{email} is now an administrator.", ok=True
    )


@router.post("/admin/users/{user_id}/block")
async def post_user_block(
    request: Request,
    db: DBDep,
    user: Admin,
    user_id: int,
    reason: Annotated[str, Form()] = "",
    q: Annotated[str, Form()] = "",
):
    """Shut an account out of the site.

    Refusing on an administrator is `block_user`'s job, not this route's, so
    that the rule and the write are one statement - see it for why. `False`
    comes back for an administrator and for an id that is not there, and both
    get the same sentence, because in both cases nothing happened.
    """
    reason = reason.strip()
    if not await block_user(db, user_id, reason or None):
        return await _render_users(
            request,
            db,
            user,
            q,
            message="Nobody was blocked: an administrator cannot be.",
        )
    await db.commit()
    return await _render_users(
        request, db, user, q, message="That account is now blocked.", ok=True
    )


@router.post("/admin/users/{user_id}/unblock")
async def post_user_unblock(
    request: Request,
    db: DBDep,
    user: Admin,
    user_id: int,
    q: Annotated[str, Form()] = "",
):
    """Let an account back in."""
    if not await unblock_user(db, user_id):
        return await _render_users(
            request, db, user, q, message="There is no such account."
        )
    await db.commit()
    return await _render_users(
        request, db, user, q, message="That account is welcome again.", ok=True
    )


@router.post("/admin/users/{user_id}/password/reset")
async def post_user_password_reset(
    request: Request,
    db: DBDep,
    user: Admin,
    user_id: int,
    q: Annotated[str, Form()] = "",
):
    """Issue a fresh password for an account, and show it once.

    This is the whole answer to "I have forgotten my password", and it is why
    no SMTP setting exists anywhere in this app: cocco cannot send a reset
    link, so a human does instead. The password is generated rather than
    chosen - `mint_token`'s discipline, and for the same reason: an
    administrator picking one would pick a memorable one, and would pick the
    same one twice.

    An administrator does see it, which is a real and deliberate cost. They can
    already appoint and block, so this is not a new kind of power over an
    account, but it *is* a credential in somebody else's name, which is why the
    fragment tells them to have its owner replace it and why the profile page
    is where that happens. `first_login_at` is untouched, so this never
    re-opens the account to a claim by somebody else.
    """
    row = await query_user(db, user_id)
    if row is None:
        return await _render_users(
            request, db, user, q, message="There is no such account."
        )

    password = generate_password()
    await set_user_password(
        db, user_id, await run_in_threadpool(hash_password, password)
    )
    await db.commit()
    logger.warning(
        "%s issued a new password for %s", user.email, row.email
    )
    return await _render_users(
        request,
        db,
        user,
        q,
        message=f"A new password was issued for {row.email}.",
        ok=True,
        issued=(row.email, password),
    )
