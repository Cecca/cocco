"""The profile page: nickname, avatar, API tokens and the password."""

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import (
    Response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.concurrency import run_in_threadpool

from cocco import schema
from cocco.avatar import MAX_AVATAR_BYTES, generate_identicon, sniff_image_mime
from cocco.model import (
    User,
)
from cocco.passwords import (
    MIN_PASSWORD_LENGTH,
    check_password,
    hash_password,
    verify_password,
)
from cocco.repository.credentials import (
    delete_api_token,
    delete_user_password,
    insert_api_token,
    query_api_tokens,
    query_password_hash,
    query_password_state,
    set_user_password,
)
from cocco.repository.users import (
    is_administrator,
    query_user,
    query_user_profile,
    update_user,
)
from cocco.tokens import hash_token, mint_token
from cocco.web import (
    core,
)
from cocco.web.access import (
    CurrentUser,
    Forbidden,
)
from cocco.web.core import (
    DBDep,
)
from cocco.web.signin import _configured_oauth_providers, _require_password_login
from cocco.web.templating import (
    templates,
)

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# The profile
# ═══════════════════════════════════════════════════════════════════════════
#
# One URL and two pages, the way `get_challenge` is: `/users/{id}` is the
# profile, and who is asking decides whether the fields are text or a form.
# The owner may change everything but the address; a site administrator reads
# the same page and cannot touch it.
#
# That asymmetry is the point, and it is why there are two dependencies rather
# than one. `/admin` is about what an account may *do* - appoint it, block it -
# and those are powers over somebody. What they are called and what their face
# looks like is not a power, it is theirs; an administrator who needs to change
# it can look at the page and ask. The address is nobody's to edit: it is the
# login identifier a provider proves, and `user.email` is unique on it.
#
# None of these routes collide with a path parameter elsewhere, so their
# position in this file is not load-bearing the way `/courses/new`'s is.


async def require_profile_reader(user_id: int, db: DBDep, user: CurrentUser) -> bool:
    """Authorize a look at a profile, and answer whose it is.

    Returns `can_edit`, so the route asks once and the template does not have
    to work it out again - the same trick `authorize_submission` plays with
    `is_staff`. A profile is one real person's name, face and class list, so
    the two who may see it are that person and a site administrator; being
    course staff somewhere they happen to be enrolled is not enough, because
    the page spans every course rather than the one in a URL.
    """
    if user.id == user_id:
        return True
    if await is_administrator(db, user.email):
        return False
    raise Forbidden("You can only look at your own profile.")


ProfileReader = Annotated[bool, Depends(require_profile_reader)]


async def require_profile_owner(user_id: int, user: CurrentUser) -> schema.User:
    """Authorize a change to a profile: only ever your own.

    Deliberately without the administrator branch `require_profile_reader` has.
    Reading somebody's profile is oversight; renaming them is impersonation,
    and there is no button on any page that asks for it.
    """
    if user.id != user_id:
        raise Forbidden("You can only edit your own profile.")
    return user


ProfileOwner = Annotated[schema.User, Depends(require_profile_owner)]

# The longest nickname the navbar can show without wrapping, and the same
# ceiling `nickname_from_email` truncates to when it derives one.
MAX_NICKNAME_CHARS = 64


async def _render_profile_identity(
    request: Request,
    db: AsyncConnection,
    user_id: int,
    message: str | None = None,
    ok: bool = False,
):
    """The name and the face, as every mutation on the profile answers with it.

    One swap target, alert included, for the reason `/admin` has one: the
    sentence and the new state arrive together instead of being kept in step.
    It covers exactly what a mutation here can change - deliberately not the
    address, the badges or the courses, none of which any button on this page
    touches, and all of which would drag `query_user_profile` in behind it.
    """
    row = await query_user(db, user_id)
    if row is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="f-profile-identity.jinja",
        context={
            "profile": row,
            # Every route that renders this fragment is behind `ProfileOwner`.
            "can_edit": True,
            "message": message,
            "ok": ok,
        },
    )


@router.get("/users/{user_id}")
async def get_profile(
    user_id: int, request: Request, db: DBDep, user: CurrentUser, can_edit: ProfileReader
):
    """What the site knows about one person: their address, their name, their
    face and the courses they are on."""
    profile = await query_user_profile(db, user_id)
    if profile is None:
        raise HTTPException(status_code=404)
    # `can_edit` is the owner *or* an administrator; the token section is the
    # owner alone, which is a narrower question than the page has asked so far
    # and so needs its own answer. Both are presentation - the routes behind
    # every form here are guarded by `ProfileOwner` regardless.
    is_owner = user.id == user_id
    return templates.TemplateResponse(
        request=request,
        name="profile.jinja",
        context={
            "profile": profile,
            "can_edit": can_edit,
            "is_owner": is_owner,
            "profile_id": user_id,
            "tokens": await query_api_tokens(db, user_id) if is_owner else [],
            "minted": None,
            # The password section, on the same `is_owner` footing as the
            # tokens above and behind the same switch the login page is: with
            # `[auth] password_login` off the feature does not exist, and the
            # routes 404 rather than merely not being drawn.
            "password_login": core.CONFIG.auth.password_login,
            "password": (
                await query_password_state(db, user_id)
                if is_owner and core.CONFIG.auth.password_login
                else None
            ),
            "min_password_length": MIN_PASSWORD_LENGTH,
            # `f-password.jinja` says different things about removing a
            # password depending on whether there is another way in.
            "sso_configured": bool(_configured_oauth_providers()),
            "title": profile["nickname"],
            "user": user,
            "message": None,
            "ok": False,
        },
    )


@router.post("/users/{user_id}/profile")
async def post_profile(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    # Defaulted rather than required, as on `/admin`: a missing field is a 422,
    # and htmx drops the body of an error response, so the person who cleared
    # the box would see nothing at all instead of the sentence below.
    nickname: Annotated[str, Form()] = "",
):
    """Save the display name.

    The avatar is not regenerated to match. It is seeded on the nickname when
    the account is created, but once somebody has a face it is theirs and a
    rename must not silently redraw it - the button below is how it changes.
    """
    nickname = nickname.strip()
    if not nickname:
        return await _render_profile_identity(
            request, db, user_id, message="A profile needs a name."
        )
    if len(nickname) > MAX_NICKNAME_CHARS:
        return await _render_profile_identity(
            request,
            db,
            user_id,
            message=f"That name is longer than {MAX_NICKNAME_CHARS} characters.",
        )

    await update_user(db, user_id, nickname=nickname)
    await db.commit()
    return await _render_profile_identity(request, db, user_id, "Saved.", ok=True)


@router.post("/users/{user_id}/avatar")
async def post_avatar(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    avatar_file: Annotated[UploadFile, File()],
):
    """Replace the avatar with an uploaded picture.

    The mime stored beside the bytes is `sniff_image_mime`'s answer and never
    the upload's own `content_type` header, which is written by whoever is
    uploading: `user_avatar` below serves those bytes back under that mime from
    this origin, so believing the header would be believing a stranger about
    what the browser should execute.
    """

    async def refuse(message: str):
        return await _render_profile_identity(request, db, user_id, message=message)

    def too_big(size: int) -> str:
        return (
            f"That picture is {size / 1024:.0f} KB — the limit is "
            f"{MAX_AVATAR_BYTES // 1024} KB."
        )

    # Checked before the read, so a huge upload is refused rather than
    # buffered. `size` may be absent, hence the second check below.
    if avatar_file.size is not None and avatar_file.size > MAX_AVATAR_BYTES:
        return await refuse(too_big(avatar_file.size))

    contents = await avatar_file.read()
    if len(contents) > MAX_AVATAR_BYTES:
        return await refuse(too_big(len(contents)))
    if not contents:
        return await refuse("That file is empty.")

    mime = sniff_image_mime(contents)
    if mime is None:
        return await refuse("That is not a PNG, JPEG, GIF or WebP picture.")

    await update_user(db, user_id, image=contents, image_mime=mime)
    await db.commit()
    return await _render_profile_identity(
        request, db, user_id, "That is your avatar now.", ok=True
    )


@router.post("/users/{user_id}/avatar/reset")
async def post_avatar_reset(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    """Go back to the generated identicon.

    Seeded on the nickname as it is *now*, not as it was when the account was
    created, so this doubles as the way to make the face follow a rename.
    """
    image, image_mime = generate_identicon(owner.nickname)
    await update_user(db, user_id, image=image, image_mime=image_mime)
    await db.commit()
    return await _render_profile_identity(
        request, db, user_id, "Your avatar is generated from your name again.", ok=True
    )


########################################################################
# API tokens, which live on the profile because they are the account's, not the
# site's. Every route here is behind `ProfileOwner` and deliberately *not*
# `ProfileReader`, the read half - which is the same line that page already
# draws, one step further along. Reading a profile is oversight; minting a
# credential in somebody else's name is impersonation, and even *listing*
# somebody's tokens is reading the shape of their credentials rather than
# anything about them. An administrator who needs one of these gone has the
# same escape hatch the other two have: `DELETE FROM api_token WHERE ...`.

# Long enough to say what a token is for, short enough for the table.
MAX_TOKEN_LABEL_CHARS = 60


async def _render_tokens(
    request: Request,
    db: AsyncConnection,
    user_id: int,
    message: str | None = None,
    ok: bool = False,
    minted: str | None = None,
):
    """The token list, as every mutation answers with it.

    One swap target with the alert folded in, which is `/admin`'s rule. `minted`
    is the one thing this fragment shows that no query can produce: the token
    itself, on the single response that created it. Only its hash is stored, so
    there is nowhere it could be read back from - which is the point, and which
    is why the fragment says so where it prints it.
    """
    return templates.TemplateResponse(
        request=request,
        name="f-tokens.jinja",
        context={
            "profile_id": user_id,
            "tokens": await query_api_tokens(db, user_id),
            "message": message,
            "ok": ok,
            "minted": minted,
        },
    )


@router.get("/f/users/{user_id}/tokens")
async def get_fragment_tokens(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    return await _render_tokens(request, db, user_id)


@router.post("/users/{user_id}/tokens")
async def post_token(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    # Defaulted rather than required, as on `/admin` and on the profile form
    # above: a missing field is a 422, and htmx drops the body of an error
    # response, so the person who left the box empty would see nothing at all.
    label: Annotated[str, Form()] = "",
):
    """Mint a token, and show it once."""
    label = label.strip()
    if not label:
        return await _render_tokens(
            request, db, user_id, "Give the token a label, so you know what to "
            "revoke later.", ok=False,
        )
    if len(label) > MAX_TOKEN_LABEL_CHARS:
        return await _render_tokens(
            request, db, user_id,
            f"Labels are at most {MAX_TOKEN_LABEL_CHARS} characters.", ok=False,
        )

    token = mint_token()
    await insert_api_token(db, user_id, label, hash_token(token))
    await db.commit()
    return await _render_tokens(
        request, db, user_id,
        "Token created. Copy it now - it is not shown again.", ok=True,
        minted=token,
    )


@router.post("/users/{user_id}/tokens/{token_id}/delete")
async def post_token_delete(
    user_id: int,
    token_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
):
    """Revoke a token. It stops working on the very next call, because the
    lookup in `get_api_user` is a read of the row this deletes."""
    removed = await delete_api_token(db, user_id, token_id)
    await db.commit()
    return await _render_tokens(
        request, db, user_id,
        "Token revoked." if removed else "That token is already gone.",
        ok=removed,
    )


########################################################################
# The password, which lives beside the tokens because it is the same kind of
# thing: a credential of this account, on this account's own page. Every route
# here is behind `ProfileOwner` for the tokens' reason, one step further along
# - reading a profile is oversight, and setting somebody's password is not
# oversight but the ability to become them. An administrator who has to get an
# account back in uses `/admin`'s reset, which issues a password rather than
# choosing one, and which the owner is expected to replace from here.
#
# All of it is behind `[auth] password_login` as well, and a 404 rather than a
# hidden form, the rule `post_auth_dummy` states.


async def _render_password(
    request: Request,
    db: AsyncConnection,
    user_id: int,
    message: str | None = None,
    ok: bool = False,
):
    """The password section, as every mutation answers with it - one swap
    target with the alert folded in, which is `/admin`'s rule and
    `_render_tokens`'s shape."""
    return templates.TemplateResponse(
        request=request,
        name="f-password.jinja",
        context={
            "profile_id": user_id,
            "password": await query_password_state(db, user_id),
            "min_password_length": MIN_PASSWORD_LENGTH,
            "sso_configured": bool(_configured_oauth_providers()),
            "message": message,
            "ok": ok,
        },
    )


@router.get("/f/users/{user_id}/password")
async def get_fragment_password(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    _require_password_login()
    return await _render_password(request, db, user_id)


@router.post("/users/{user_id}/password")
async def post_password(
    user_id: int,
    request: Request,
    db: DBDep,
    owner: ProfileOwner,
    # Defaulted, as everywhere else on this page: a missing field is a 422 and
    # htmx drops the body of an error response.
    current_password: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    """Set a password, or change the one that is there.

    The current password is required whenever there already is one, and is not
    a formality: a session left open on a shared machine is the ordinary way an
    account is taken, and without this check the person who found it could lock
    its owner out. There is nothing to ask for when no password is set yet -
    the session is the only thing that could have got here.
    """
    _require_password_login()

    existing = await query_password_state(db, user_id)
    if existing is not None:
        if not await run_in_threadpool(
            verify_password,
            current_password,
            await query_password_hash(db, owner.email),
        ):
            return await _render_password(
                request, db, user_id, "That is not your current password.", ok=False
            )

    try:
        check_password(password)
    except ValueError as err:
        return await _render_password(request, db, user_id, str(err), ok=False)

    if password != confirm:
        return await _render_password(
            request, db, user_id, "Those two passwords are not the same.", ok=False
        )

    await set_user_password(
        db, user_id, await run_in_threadpool(hash_password, password)
    )
    await db.commit()
    return await _render_password(
        request,
        db,
        user_id,
        "Password changed." if existing is not None else "Password set.",
        ok=True,
    )


@router.post("/users/{user_id}/password/delete")
async def post_password_delete(
    user_id: int, request: Request, db: DBDep, owner: ProfileOwner
):
    """Take the password off this account.

    Allowed even when it is the only way in, and deliberately: this is the
    owner's own account and the fragment says plainly what they are about to be
    left with. The app's usual line applies - nothing else goes with it, and
    `/admin` can issue a new one.
    """
    _require_password_login()

    removed = await delete_user_password(db, user_id)
    await db.commit()
    return await _render_password(
        request,
        db,
        user_id,
        "Password removed." if removed else "There was no password to remove.",
        ok=removed,
    )


@router.get("/users/{user_id}/avatar")
async def user_avatar(user_id: int, db: DBDep, user: CurrentUser):
    row = (
        await db.execute(
            select(User.c.image, User.c.image_mime).where(User.c.id == user_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404)
    return Response(
        content=row.image,
        media_type=row.image_mime,
        headers={
            # These bytes were uploaded by a user and are served from the same
            # origin as the app, so the browser is told not to guess at their
            # type and not to let them reach anything. `sniff_image_mime`
            # already refuses everything but the four raster formats; this is
            # the second lock on the same door.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # The URL does not change when the picture does, so without this a
            # new avatar would keep showing as the old one until a hard reload.
            "Cache-Control": "no-cache",
        },
    )
