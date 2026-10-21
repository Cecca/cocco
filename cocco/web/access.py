"""Who is asking, and the gates a route declares against them."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.auth import safe_next_path
from cocco.model import UserRole
from cocco.repository.challenges import challenge_in_course
from cocco.repository.courses import query_course_role, query_user_role
from cocco.repository.credentials import query_api_token_user, touch_api_token
from cocco.repository.lifecycle import query_challenge_visibility
from cocco.repository.users import is_administrator, query_user
from cocco.tokens import hash_token, parse_bearer
from cocco.web.core import DBDep, EDITOR_ROLES
from cocco.web.templating import render_datetime, templates


########################################################################
# dependency injection
class NotAuthenticated(Exception):
    """Raised by `get_user` when there is no session behind a request.

    Not an `HTTPException`, because the right answer depends on who is asking:
    a browser wants a redirect, htmx wants a header. See the handler below.
    """

    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


def _refuse_if_blocked(user: schema.User) -> None:
    """The site-wide gate, asked of the row `get_user` has already read.

    Free, which is why the block lives on `user` rather than in a table of its
    own, and asked again on every request, so it takes effect on the blocked
    person's very next click - exactly as a deleted account does.
    """
    if user.blocked_at is not None:
        raise Blocked(user.blocked_reason)


async def get_user(request: Request, db: DBDep) -> schema.User:
    """The signed-in user, or `NotAuthenticated` - or `Blocked`.

    The cookie holds nothing but the user id, so a renamed - or deleted, or
    blocked - account takes effect on the very next request.

    The user is also stashed on `request.state`, for the one caller that cannot
    ask for a dependency: `handle_forbidden` renders a whole page, navbar
    included, and this has always run by the time an authorization dependency
    can refuse. The block is checked *before* the stash, deliberately - see
    `handle_blocked`, which wants no viewer at all.

    There is **one branch**, and that is the point of `post_auth_dummy` being a
    sign-in rather than a bypass: development used to resolve an address out of
    the configuration on every request, which made this function two functions
    and meant every rule here - the block, the stale session, the redirect - had
    to be written twice and could be got wrong in the half nobody browses in.
    """
    user_id = request.session.get("user_id")
    if user_id is not None:
        row = await query_user(db, user_id)
        if row is not None:
            user = schema.User.model_validate(row)
            _refuse_if_blocked(user)
            request.state.user = user
            return user
        # A session pointing at nobody: in development, typically the other
        # side of a `[dev] reset_db`.
        request.session.clear()
    raise NotAuthenticated(_return_path(request))


CurrentUser = Annotated[schema.User, Depends(get_user)]


def _return_path(request: Request) -> str:
    """Where to send the visitor once they have signed in.

    Only a plain GET of a page is worth coming back to: replaying a POST after
    a login is not something to attempt, and a polled fragment is not a
    destination - its page is.
    """
    if request.method != "GET" or request.headers.get("HX-Request") == "true":
        return "/"
    return safe_next_path(request.url.path)


async def handle_not_authenticated(request: Request, exc: NotAuthenticated):
    login_url = str(
        request.url_for("get_login").include_query_params(next=exc.next_path)
    )
    if request.headers.get("HX-Request") == "true":
        # htmx follows a 302 inside the XHR, so a redirect here would splice the
        # login page into whatever div was polling - every two seconds, for as
        # long as the tab stays open. HX-Redirect navigates the browser instead.
        return Response(status_code=204, headers={"HX-Redirect": login_url})
    return RedirectResponse(login_url, status_code=303)


class Forbidden(HTTPException):
    """A 403 that renders as a page.

    Its own class rather than a plain `HTTPException` so the handler below can
    be registered on it alone: FastAPI's defaults still answer everything else,
    and a 404 for an unparseable id does not go through a template.

    `detail` is written for the person reading it, not for a log - it is the
    sentence the page shows.
    """

    def __init__(self, detail: str):
        super().__init__(status_code=403, detail=detail)


async def handle_forbidden(request: Request, exc: Forbidden):
    """The refusal, as a page.

    Enrollment is the gate on every course, so a 403 is now something an
    ordinary visitor can reach by clicking rather than by typing a URL, and raw
    JSON is not an answer for them. Polled fragments are unharmed: htmx does not
    swap the body of an error response, so a fragment behind a course the viewer
    left simply stops updating.
    """
    return templates.TemplateResponse(
        request=request,
        name="403.jinja",
        status_code=403,
        context={
            "message": exc.detail,
            "title": "Not allowed",
            # Stashed by `get_user`; absent only if something refused before
            # anyone was identified, and `base.jinja` guards the navbar anyway.
            "user": getattr(request.state, "user", None),
        },
    )


class Blocked(Forbidden):
    """An account an administrator has shut out of the site.

    A subclass of `Forbidden` rather than a fresh `HTTPException`: it is still
    "a 403 that renders as a page", and Starlette resolves a handler along the
    exception's MRO, so deleting the handler below would degrade this to
    `403.jinja` rather than to raw JSON.

    Its own class all the same, because it is not a refusal about the URL that
    was asked for - nothing on the site is open to them - and the page has to
    say so and carry the sentence somebody wrote for them.
    """

    DEFAULT_REASON = "No reason was recorded."

    def __init__(self, reason: str | None):
        self.reason = reason or self.DEFAULT_REASON
        super().__init__(f"This account is blocked. {self.reason}")


async def handle_blocked(request: Request, exc: Blocked):
    """The refusal a blocked account gets, on every request it makes.

    Rendered with no viewer, and that is not modesty: the navbar's avatar is
    `/users/{id}/avatar`, itself behind `CurrentUser`, so it would 403 into a
    broken image on the very page explaining the block. `get_user` raises
    before it stashes `request.state.user`, which is what makes that possible.

    The session is left alone. Clearing it would turn every later request into
    `NotAuthenticated` and send them to a login page that explains nothing,
    while this page explains everything - and the cookie holds only an id,
    which is re-checked here on every request anyway. `get_user` still clears a
    session pointing at *nobody*: that one is stale, this one is not.

    Polled fragments are left to freeze, as they do for a student unenrolled
    mid-session: htmx does not swap the body of an error response. Deliberately
    not the `HX-Redirect` `handle_not_authenticated` sends, which would conflate
    being blocked with being signed out and land them on the one page that
    cannot tell them what happened.
    """
    return templates.TemplateResponse(
        request=request,
        name="blocked.jinja",
        status_code=403,
        context={
            "title": "Account blocked",
            "reason": exc.reason,
            "user": None,
        },
    )


async def require_admin(db: DBDep, user: CurrentUser) -> schema.User:
    """Authorize a site administrator, or raise 403.

    The only permission in the app that names no course - which is exactly why
    it is not a `UserRole`: it is a row in `administrator`, keyed on the
    address, put there by `cocco.toml`, by the admin page or by hand. It guards
    creating a course, where the course does not exist yet to have a role on,
    and the admin page, which is about the site rather than about any course.

    The sentence names the permission rather than either of those two, so a
    third one does not find the wrong refusal waiting for it.

    Returns the user, so a route that needs the creator does not ask twice.
    Hiding the button on the home page is an affordance, not this check.
    """
    if not await is_administrator(db, user.email):
        raise Forbidden("Only a site administrator can do that.")
    return user


Admin = Annotated[schema.User, Depends(require_admin)]


async def effective_course_role(
    db: AsyncConnection, user: schema.User, course_id: int
) -> UserRole | None:
    """The role every check on a course is made against: the enrollment's, or
    `teacher` for a site administrator, who is course staff everywhere.

    Substituting `teacher` is not a fiction bolted on here - it is already this
    app's spelling of full staff on a course, and the very row `post_course_new`
    writes for the administrator who creates one. Saying it in one place is what
    keeps every `role in EDITOR_ROLES` downstream, in Python and in the
    templates, true without being touched.

    The administrator lookup only runs when the enrollment did not already
    answer the question, so course staff pay nothing for it.
    """
    role = await query_course_role(db, user_id=user.id, course_id=course_id)
    if role in EDITOR_ROLES:
        return role
    if await is_administrator(db, user.email):
        return UserRole.teacher
    return role


async def effective_challenge_role(
    db: AsyncConnection, user: schema.User, challenge_id: int
) -> UserRole | None:
    """`effective_course_role` for the routes whose URL carries only a
    challenge, resolving the course through it exactly as `query_user_role`
    does."""
    role = await query_user_role(db, user_id=user.id, challenge_id=challenge_id)
    if role in EDITOR_ROLES:
        return role
    if await is_administrator(db, user.email):
        return UserRole.teacher
    return role


async def require_course_editor(
    course_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """Authorize course staff on `course_id`, or raise 403.

    Keyed on the course rather than on a challenge, so it also answers the one
    question a challenge cannot: whether this person may create one here.
    Hiding a button in the template is a UI affordance, not access control.
    """
    role = await effective_course_role(db, user, course_id)
    if role is None or role not in EDITOR_ROLES:
        raise Forbidden("Only this course's staff can do that.")
    return role


CourseEditor = Annotated[UserRole, Depends(require_course_editor)]


async def require_course_member(
    course_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """Authorize anyone enrolled on `course_id`, of any role, or raise 403.

    This is the gate on a course's contents. `EDITOR_ROLES` is a subset of
    "enrolled", so staff pass it without a second check, and it is deliberately
    the same `effective_course_role` `require_course_editor` uses: the role is
    per course, so the course in the path is the key - and a site administrator
    is staff on every course, so they are inside every gate.

    What it does *not* guard is the course page itself - `get_course` shows a
    non-member the title, the description and the way in - nor the home page,
    which has to go on listing every course or nobody could find one to enroll
    on, nor `post_course_enroll`, which is that way in.
    """
    role = await effective_course_role(db, user, course_id)
    if role is None:
        raise Forbidden("You are not enrolled on this course.")
    return role


CourseMember = Annotated[UserRole, Depends(require_course_member)]


async def require_challenge_visible(
    db: AsyncConnection, challenge_id: int, role: UserRole
) -> None:
    """Refuse a challenge the class has not been given yet, or return.

    The whole enforcement of hiding, and it lives here - inside the two gates
    that already answer "may this person read this challenge" - rather than
    scattered over the five routes behind them. `post_submission` in particular
    needs no check of its own: its dependency runs before its body, so a hidden
    challenge refuses an upload without reading a byte of it. That is the
    opposite arrangement to archiving, which is a state a route has to consult
    (`query_effective_archived_at`) because an archived challenge is still one
    you may open.

    Course staff pass whatever the state is - the editor is where a challenge is
    written, and it would be a strange hiding place that hid it from them. For
    everybody else the two refusals are deliberately different. A hidden
    challenge is a bare 404, which tells the asker nothing, and matches what the
    course page already showed them: no such thing. A *scheduled* one is a
    sentence with the date in it, because its card is on their course page
    saying exactly that, and a 404 there would read as a broken link.
    """
    if role in EDITOR_ROLES:
        return
    visible_from, visible = await query_challenge_visibility(db, challenge_id)
    if visible:
        return
    if visible_from is None:
        raise HTTPException(status_code=404)
    raise Forbidden(f"This challenge opens on {render_datetime(visible_from)}.")


async def require_challenge_viewer(
    challenge_id: int, course_id: int, db: DBDep, role: CourseMember
) -> UserRole:
    """Authorize reading `challenge_id` under `course_id`, or raise 403 - or 404.

    `require_challenge_editor` with the membership gate in place of the staff
    one, and for the same reason: the role comes from the course in the path, so
    the challenge in the path has to be checked against it. Otherwise a student
    of one course could name their own course in front of another course's
    challenge and read it. A mismatched pair is a URL for a challenge that does
    not exist under that course, hence 404.

    The visibility check comes after the cross-check and not before it: a
    student naming somebody else's course is a 404 whatever that challenge's
    visibility is, and asking the second question first would leak which of two
    reasons applied.
    """
    if not await challenge_in_course(db, challenge_id, course_id):
        raise HTTPException(status_code=404)
    await require_challenge_visible(db, challenge_id, role)
    return role


ChallengeViewer = Annotated[UserRole, Depends(require_challenge_viewer)]


async def require_challenge_member(
    challenge_id: int, db: DBDep, user: CurrentUser
) -> UserRole:
    """The same gate, for the routes whose URL names only a challenge.

    Keyed on the challenge because there is nothing else to key on: these are
    the fragments and the sample download, whose paths carry no `course_id`.
    That is not the mistake `require_challenge_editor` avoids - there the course
    is in the path too, and staff of one course naming it in front of another's
    challenge is a real attack. With one id there is no second key to disagree
    with, and the challenge determines its course.

    A site administrator passes this without an enrollment, and so also passes
    it for a challenge id that does not exist at all - the route's own query
    then finds nothing, which is the empty fragment it would have rendered for
    a challenge whose contents are gone anyway.
    """
    role = await effective_challenge_role(db, user, challenge_id)
    if role is None:
        raise Forbidden("You are not enrolled on the course this challenge belongs to.")
    await require_challenge_visible(db, challenge_id, role)
    return role


ChallengeMember = Annotated[UserRole, Depends(require_challenge_member)]


async def require_challenge_editor(
    challenge_id: int, course_id: int, db: DBDep, role: CourseEditor
) -> UserRole:
    """Authorize an edit of `challenge_id`, or raise 403 - or 404.

    The role comes from the course in the path, so the challenge in the path
    has to be checked against it rather than assumed: otherwise staff of one
    course could name their own course in front of someone else's challenge
    and edit it. A mismatched pair is a URL for a challenge that does not
    exist under that course, hence 404 rather than 403.
    """
    if not await challenge_in_course(db, challenge_id, course_id):
        raise HTTPException(status_code=404)
    return role


ChallengeEditor = Annotated[UserRole, Depends(require_challenge_editor)]


########################################################################
# The API's own two dependencies.
#
# Everything above answers a browser: `NotAuthenticated` becomes a 303 to
# `/login` or an `HX-Redirect`, and `Forbidden` renders `403.jinja` through a
# template. Both are nonsense to a program - a CLI following that 303 would be
# handed a login page with a 200 on it - so the API refuses with real status
# codes and a JSON body, which is what FastAPI's own handler does for a plain
# `HTTPException`.


class ApiNotAuthenticated(HTTPException):
    """A 401 for a caller that has no browser to send anywhere.

    Deliberately not `NotAuthenticated`, whose handler is the whole reason that
    class is not an `HTTPException` in the first place: it has to decide between
    a redirect and a header, and neither is an answer here.
    """

    def __init__(self, detail: str):
        super().__init__(
            status_code=401,
            detail=detail,
            # The scheme, so a generic HTTP client knows what it was supposed
            # to send rather than only that it was turned away.
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_api_user(request: Request, db: DBDep) -> schema.User:
    """Who is calling the API: a bearer token, or the browser's own session.

    Two ways in, in this order.

    `Authorization: Bearer`, which is what a command line actually uses. The
    token is never stored, so the lookup is by its hash - which means an unknown
    token and a revoked one are the same miss, as they should be.

    Then the session cookie, so a signed-in browser can follow a plain Download
    link to an export URL. That fallback is safe on the mutating half for the
    same reason every other POST in this app is: `same_site="lax"` means the
    session cookie is not sent on a cross-site POST at all, and that is the
    whole CSRF defence here as everywhere else.

    There is deliberately no `DUMMY_AUTH` branch, and there used to be: it came
    first and signed every API call in as the configured address, which meant a
    bearer token presented to a development server was *ignored* - the one
    configuration where the header this function is mostly about did nothing.
    The cost is that `cocco pull` against a development server now needs a token like
    anywhere else: mint one on the profile page and put it in `COCCO_TOKEN`.
    """
    presented = parse_bearer(request.headers.get("Authorization"))
    if presented is not None:
        token_hash = hash_token(presented)
        row = await query_api_token_user(db, token_hash)
        if row is None:
            raise ApiNotAuthenticated("Unknown or revoked API token.")
        user = schema.User.model_validate(row)
        _refuse_if_blocked(user)
        # One UPDATE per accepted call. It makes a GET write, which is worth
        # saying out loud, and it is the only thing that can tell a token's
        # owner it has gone stale before they revoke it.
        await touch_api_token(db, token_hash)
        await db.commit()
        request.state.user = user
        return user

    user_id = request.session.get("user_id")
    if user_id is not None:
        row = await query_user(db, user_id)
        if row is not None:
            user = schema.User.model_validate(row)
            _refuse_if_blocked(user)
            request.state.user = user
            return user

    raise ApiNotAuthenticated(
        "No API token. Mint one on your profile page and send it as "
        "`Authorization: Bearer <token>`."
    )


ApiUser = Annotated[schema.User, Depends(get_api_user)]


async def require_api_challenge_editor(
    challenge_id: int, db: DBDep, user: ApiUser
) -> UserRole:
    """Authorize a pull or a push of `challenge_id`, or raise 403.

    Keyed on the challenge alone, because the API's URL carries nothing else -
    the same shape as `require_challenge_member`, and safe for the reason its
    docstring gives: with one id in the path there is no second key to disagree
    with, so the cross-check `require_challenge_editor` performs has nothing to
    check. It resolves through `effective_challenge_role`, so a site
    administrator is staff here exactly as they are everywhere.

    A plain `HTTPException` rather than `Forbidden`, which renders a page.
    """
    role = await effective_challenge_role(db, user, challenge_id)
    if role is None or role not in EDITOR_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only this challenge's course staff can do that.",
        )
    return role


ApiChallengeEditor = Annotated[UserRole, Depends(require_api_challenge_editor)]


async def authorize_submission(
    db: AsyncConnection, user: schema.User, submission
) -> bool:
    """Let the viewer see a submission, or raise 403. Returns True for staff.

    Not a dependency like `require_challenge_editor`, because it needs the
    submission row first and because a student *is* allowed here: the rule is
    that you may read your own work, and course staff may read anyone's.
    """
    role = await effective_challenge_role(db, user, submission["challenge_id"])
    is_staff = role in EDITOR_ROLES
    if not is_staff and submission["user_id"] != user.id:
        raise Forbidden("That submission is not yours.")
    return is_staff
