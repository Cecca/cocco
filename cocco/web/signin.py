"""Signing in: the login page, OAuth providers, dummy auth, passwords and logout."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import (
    APIRouter,
    Form,
    HTTPException,
    Request,
)
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.concurrency import run_in_threadpool

from cocco import schema
from cocco.auth import (
    get_or_create_user,
    is_allowed_email,
    normalize_email,
    oauth_redirect_uri,
    safe_next_path,
)
from cocco.passwords import (
    MIN_PASSWORD_LENGTH,
    check_password,
    hash_password,
    needs_rehash,
    verify_password,
)
from cocco.repository.credentials import (
    claim_user_password,
    query_password_hash,
    set_user_password,
)
from cocco.repository.users import (
    query_user_by_email,
    record_sign_in,
)
from cocco.web import core
from cocco.web.core import DBDep
from cocco.web.seed import (
    DUMMY_ADMIN,
    DUMMY_STUDENTS,
    DUMMY_TEACHERS,
)
from cocco.web.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


# Registered at import, but nothing is fetched here: Authlib pulls Google's
# metadata document lazily, on the first `authorize_redirect`, and GitHub has
# no metadata document at all. That is what lets a checkout with no credentials
# boot and merely say so on the login page. Each provider is registered only
# when its own two environment variables are set, which is the whole of what
# turns one on.
oauth = OAuth()
if core.CONFIG.auth.google is not None:
    oauth.register(
        name="google",
        client_id=core.CONFIG.auth.google.client_id,
        client_secret=core.CONFIG.auth.google.client_secret,
        server_metadata_url=core.CONFIG.auth.google.server_metadata_url,
        client_kwargs={
            # `profile` is deliberately not asked for: the nickname is derived
            # from the address, so the name and the picture would be data we
            # hold without using. Both of these are non-sensitive scopes, so
            # Google asks for no verification review.
            "scope": "openid email",
            "code_challenge_method": "S256",
        },
    )


if core.CONFIG.auth.github is not None:
    oauth.register(
        name="github",
        client_id=core.CONFIG.auth.github.client_id,
        client_secret=core.CONFIG.auth.github.client_secret,
        authorize_url="https://github.com/login/oauth/authorize",
        access_token_url="https://github.com/login/oauth/access_token",
        api_base_url="https://api.github.com/",
        client_kwargs={
            # `user:email` and not `read:user`: the nickname is derived from
            # the address, so a profile scope would be data we hold without
            # using - the same reason Google is asked for `openid email` and
            # not `profile`. GitHub is plain OAuth2, so there is no PKCE
            # method to name and no ID token to come back.
            "scope": "user:email",
        },
    )


def oauth_client(provider: str):
    """The registered client for one provider, or None when it has no
    credentials.

    A function rather than a module constant so a test can replace the whole of
    a provider with a few methods; taking the name rather than there being one
    of these per provider is what keeps that seam single.
    """
    return oauth.create_client(provider)


########################################################################
# authentication
#
# A visitor proves an address by signing in with an OAuth provider - Google or
# GitHub, each enabled by its own pair of environment variables and neither
# knowing about the other - and the account is created the first time an
# accepted address comes back. None of these routes depends on `CurrentUser`:
# they are the way in.


def _base_url(request: Request) -> str:
    """The absolute origin a provider must redirect back to.

    Configured explicitly when there is a proxy in front, since only the
    deployment knows the public name - and a proxy that terminates TLS makes
    `request.base_url` say `http`, which a provider would refuse as a mismatch.
    """
    if core.CONFIG.base_url:
        return core.CONFIG.base_url
    return str(request.base_url).rstrip("/")


def _login_page(
    request: Request,
    *,
    next_path: str = "/",
    error: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request=request,
        name="login.jinja",
        status_code=status_code,
        context={
            "title": "Sign in",
            # `base.jinja` guards on this: there is nobody to put in the navbar.
            "user": None,
            "next": next_path,
            "error": error,
            # The single-sign-on buttons: one per configured provider, so the
            # template loops rather than growing a block each time one is
            # added. A pair per provider and not the descriptor itself - a
            # template has no business holding the resolver.
            "oauth_providers": [
                (p.name, p.label) for p in _configured_oauth_providers()
            ],
            # Draws the address-and-password form and the account it can
            # create. `[auth] password_login` and nothing else, the way
            # `dummy_auth` below works: the routes 404 without it, because a
            # hidden form is presentation and never the check.
            "password_login": core.CONFIG.auth.password_login,
            "min_password_length": MIN_PASSWORD_LENGTH,
            "allowed_domains": core.CONFIG.auth.allowed_domains,
            # Draws the development sign-in box. See `post_auth_dummy`.
            "dummy_auth": core.DUMMY_AUTH,
            # The seeded cast, so the box can say who there is to be. Passed
            # from here rather than spelled in the template, which is what stops
            # it drifting from the addresses `_populate_dummy_data` writes.
            "dummy_admin": DUMMY_ADMIN,
            "dummy_teachers": DUMMY_TEACHERS,
            "dummy_students": DUMMY_STUDENTS,
        },
    )


def _sign_in(request: Request, user: schema.User, next_path: str):
    # Never carry a prior session over into a new identity - and this is also
    # what drops the OAuth state Authlib left behind, now spent.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse(next_path, status_code=303)


async def _complete_sign_in(
    request: Request,
    db: AsyncConnection,
    email: str,
    next_path: str,
    *,
    provider: str,
    claim_password: str | None = None,
):
    """Everything between a proved address and a session, for every one of the
    ways an address is proved.

    `provider` names which one - "google", "github", "password", "dummy" - and
    is only ever written to the log, where "every refused sign-in through
    GitHub" should be a filter rather than a guess.

    A provider's callback proves one - Google by verifying an ID token, GitHub
    by asking which of the account's addresses are verified; `post_auth_password`
    proves one by a password already on the account; `post_auth_dummy` "proves"
    one by being development. What has to happen next is the same three rules
    every way - the domain allowlist, the account, the blacklist - and they are
    written here once rather than restated at each end, the way
    `_challenge_fields` is shared by the three forms that build a challenge. A
    rule the browser enforced and the box did not would be a difference nobody
    could explain, and the box is the configuration this all gets developed in.

    The address must already be through `normalize_email`: canonicalizing it is
    each caller's, because the sentence for an unusable one differs - a
    provider returning no address is not the same event as an empty text box.

    `claim_password` is registration, and it is a keyword here rather than a
    route of its own precisely so that it cannot skip any of the above: the
    allowlist is checked *before* `get_or_create_user` writes anything, so an
    address outside the domains never becomes a row, and the claim happens
    before the blacklist refusal so that a blocked account still refuses.
    """
    if not is_allowed_email(email, core.CONFIG.auth.allowed_domains):
        logger.info(
            "refused a sign-in from outside the allowed domains: %s",
            email,
            extra={"email": email, "provider": provider, "reason": "domain"},
        )
        domains = ", ".join("@" + d for d in core.CONFIG.auth.allowed_domains)
        return _login_page(
            request,
            next_path=next_path,
            error=(
                f"{email} is not an institutional address. "
                f"Sign in with your {domains} account."
            ),
            status_code=403,
        )

    user = await get_or_create_user(db, email)

    if claim_password is not None:
        # The account may have existed already - a roster a teacher wrote out,
        # or somebody who signed in with a provider - so this is a *claim* and not
        # an insert, and `claim_user_password` refuses in its own WHERE clause
        # rather than here. False means the address is spoken for.
        if not await claim_user_password(db, email, claim_password):
            await db.rollback()
            logger.info(
                "refused a password claim on a taken address: %s",
                email,
                extra={"email": email, "provider": provider, "reason": "claimed"},
            )
            return _login_page(
                request,
                next_path=next_path,
                error=(
                    "That address already has an account. Sign in instead, or "
                    "ask an administrator to reset its password."
                ),
                status_code=409,
            )

    await db.commit()

    if user.blocked_at is not None:
        # Refused here as well as in `get_user`, and after the domain check so
        # that the permanent reason keeps precedence. Without this the sign-in
        # succeeds and dies on the first click, on a page they did not ask for;
        # this way the sentence arrives on the page they are already looking at.
        # `get_or_create_user` created nothing above - a blocked account exists.
        logger.info(
            "refused a sign-in from a blocked account: %s",
            email,
            extra={"email": email, "provider": provider, "reason": "blocked"},
        )
        return _login_page(
            request,
            next_path=next_path,
            error=f"This account is blocked. {user.blocked_reason or ''}".strip(),
            status_code=403,
        )

    # After the two refusals above and before the session exists: a sign-in
    # that was turned away is not one, and this function is where both ways in
    # meet, so neither can be the one that forgets.
    await record_sign_in(db, user.id)
    await db.commit()

    return _sign_in(request, user, next_path)


class _NoProvedAddress(Exception):
    """A provider came back without an address this app can accept.

    Carries the sentence and the status code, so the two resolvers below fail
    the same way and the callback has one `except` rather than a branch per
    provider.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def _google_email(client, token) -> str:
    """Google's address, out of the ID token Authlib has already verified.

    `userinfo` is its decoded claims, so the signature, the issuer, the
    audience and the nonce are all checked by the time this runs.
    """
    claims = token.get("userinfo") or {}
    try:
        email = normalize_email(claims.get("email") or "")
    except ValueError:
        raise _NoProvedAddress("Google did not return an email address.")

    if claims.get("email_verified") not in (True, "true"):
        # Google says the address is unconfirmed, so it proves nothing. Matched
        # against the two affirmative spellings rather than tested for
        # truthiness: the string "false" is truthy, and this is the kind of
        # mistake that would never fail visibly.
        raise _NoProvedAddress(
            "That Google account's email address is not verified.", 403
        )
    return email


async def _github_email(client, token) -> str:
    """GitHub's address, which has to be asked for.

    GitHub is plain OAuth2: there is no ID token and no `email_verified` claim,
    so the account's addresses come from `GET /user/emails` - the one place in
    this app where signing in makes an API call of its own.

    An account holds several, and only the **verified** ones prove anything, so
    the unverified are dropped outright and are never candidates. Among what is
    left the primary comes first, but it is not the answer on its own: a
    student's primary is usually personal while the institutional address sits
    beside it as a verified secondary, and refusing that would refuse the very
    people this is for. So the first verified address that passes
    `allowed_domains` wins.

    When none of them does, the primary is returned anyway rather than refused
    here: `_complete_sign_in` then prints its "not an institutional address"
    sentence, which names the domains and is the one a person can act on. A
    second, vaguer refusal for the same event would be a difference nobody
    could explain.
    """
    try:
        response = await client.get("user/emails", token=token)
        response.raise_for_status()
        addresses = response.json()
    except Exception:
        logger.exception("could not read the addresses of a GitHub account")
        raise _NoProvedAddress(
            "Could not reach GitHub right now. Please try again.", 502
        )

    verified = []
    for entry in addresses if isinstance(addresses, list) else []:
        if not entry.get("verified"):
            continue
        try:
            address = normalize_email(entry.get("email") or "")
        except ValueError:
            continue
        # The primary first, the rest in the order GitHub gave them.
        if entry.get("primary"):
            verified.insert(0, address)
        else:
            verified.append(address)

    if not verified:
        raise _NoProvedAddress(
            "That GitHub account has no verified email address.", 403
        )

    for address in verified:
        if is_allowed_email(address, core.CONFIG.auth.allowed_domains):
            return address
    return verified[0]


@dataclass(frozen=True)
class _OAuthProvider:
    """One way in through somebody else's login page.

    Everything the two providers share is written once, in the two routes
    below; this is the whole of what differs. `label` is the word every
    sentence and the button on the login page use, and `email` is the step that
    turns a token into a proved address - the only genuinely provider-shaped
    piece of the flow.
    """

    name: str
    label: str
    email: Callable[[object, dict], Awaitable[str]]


OAUTH_PROVIDERS = {
    "google": _OAuthProvider("google", "Google", _google_email),
    "github": _OAuthProvider("github", "GitHub", _github_email),
}


def _oauth_provider(provider: str) -> _OAuthProvider:
    """The descriptor for a URL segment, or 404.

    A **404** and not a 422, `post_auth_dummy`'s reason: a path this app does
    not serve should not be admitted to exist. Whether the provider is
    *configured* is a separate question, asked from `CONFIG.auth` at call time
    so a test that swaps the config still gets an answer about it.
    """
    descriptor = OAUTH_PROVIDERS.get(provider)
    if descriptor is None:
        raise HTTPException(status_code=404)
    return descriptor


def _configured_oauth_providers() -> list[_OAuthProvider]:
    """The providers a visitor can actually use, in registry order."""
    return [
        descriptor
        for name, descriptor in OAUTH_PROVIDERS.items()
        if getattr(core.CONFIG.auth, name) is not None
    ]


def _prune_stale_oauth_state(request: Request) -> None:
    """Drop the leftovers of abandoned sign-ins.

    Authlib stashes one entry per *started* flow in the session and removes
    only the one that finishes, so a visitor who keeps bouncing off the consent
    screen would grow the cookie without bound. Only the flow about to start
    matters - and only one of them can be in flight, so this takes every
    provider's leftovers rather than just the one being started.
    """
    for key in [k for k in request.session if k.startswith("_state_")]:
        del request.session[key]


@router.get("/login")
async def get_login(request: Request, next: str = "/"):
    return _login_page(request, next_path=safe_next_path(next))


@router.post("/auth/dummy")
async def post_auth_dummy(
    request: Request,
    db: DBDep,
    email: str = Form(""),
    next: str = Form("/"),
):
    """Sign in as whoever was typed. Development only, and hard-refused
    otherwise.

    This is the whole of `[dev] dummy_auth`. It replaces a shortcut that signed
    every request in as one address named in the configuration file, which meant
    that seeing what a student sees was an edit and a restart - on an app whose
    every permission is per course, so hopping between the seeded cast is the
    single most common thing one does while developing it.

    A **404** rather than a 403 when the switch is off: this route signs the
    caller in as anybody who can be named, so a deployment should not so much as
    admit that the path exists. Beyond that its defence is the app's only one,
    `same_site="lax"` - a cross-site POST carries no session, and there is
    nothing here worth a token that every other mutating route does without.

    Both fields are **defaulted** rather than required, for the reason
    `post_administrator`'s are: a missing one is a 422 with no page on it, and
    there is a person in front of this box who deserves a sentence.
    """
    if not core.DUMMY_AUTH:
        raise HTTPException(status_code=404)

    next_path = safe_next_path(next)
    try:
        address = normalize_email(email)
    except ValueError as err:
        return _login_page(
            request, next_path=next_path, error=str(err), status_code=400
        )
    logger.warning(
        "dummy sign-in as %s", address, extra={"email": address, "provider": "dummy"}
    )
    return await _complete_sign_in(
        request, db, address, next_path, provider="dummy"
    )


# ---------------------------------------------------------------------------
# Passwords: the third real way in, and off unless `[auth] password_login`
# says otherwise.
#
# A provider proves that the person owns the mailbox. A password proves only that
# somebody typed an address, because this app has no mail path and so cannot
# send a verification link. Registration is therefore a *claim*, narrowed as
# far as it can be without email: `claim_user_password` writes only where
# `first_login_at IS NULL`, so a roster a teacher wrote out before term can be
# claimed by its owner and an account somebody has actually used cannot be
# taken over. What is left - anyone who can type an address `allowed_domains`
# admits gets an unused one - is what a deployment accepts when it turns this
# on, and is why the default is false.
#
# Both routes 404 when the switch is off, `post_auth_dummy`'s reason: a
# deployment should not admit that a path it does not serve exists. Every field
# is a defaulted `Form`, `post_administrator`'s reason: a missing one would be
# a 422 with no page on it, and there is a person in front of this form.


def _require_password_login() -> None:
    if not core.CONFIG.auth.password_login:
        raise HTTPException(status_code=404)


# One sentence for every way a sign-in can be wrong, and it is a module
# constant so the two places that print it cannot drift. An unknown address, an
# account with no password and a wrong password are the same event as far as
# anybody outside is concerned - saying which of them it was is a user list.
_BAD_CREDENTIALS = "That address and password do not match."


@router.post("/auth/password")
async def post_auth_password(
    request: Request,
    db: DBDep,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    """Sign in with an address and a password.

    The three refusals this can make are deliberately one refusal. An address
    nobody has, an account that has never set a password and a wrong password
    all arrive at `verify_password` - the first two as None, which it takes and
    turns into False rather than short-circuiting on - so they cost the same
    work and produce the same sentence. A route that returned early for an
    unknown address would answer it faster, and a timing difference on the
    login page is a way to enumerate a class list.

    scrypt runs in a threadpool. It is 32 MiB of deliberately sequential work,
    and on the event loop it would stop every other request in the app for its
    duration - including the fragments each open page is polling.
    """
    _require_password_login()

    next_path = safe_next_path(next)
    try:
        address = normalize_email(email)
    except ValueError:
        # Not `str(err)`: an address this app would not accept is not a
        # different event from a wrong password, and saying so would tell an
        # attacker which of their guesses was even a candidate.
        address = ""

    stored = await query_password_hash(db, address) if address else None
    if not await run_in_threadpool(verify_password, password, stored):
        logger.info(
            "a password sign-in did not match: %s",
            address or email,
            extra={
                "email": address or email,
                "provider": "password",
                "reason": "bad_credentials",
            },
        )
        return _login_page(
            request, next_path=next_path, error=_BAD_CREDENTIALS, status_code=403
        )

    if needs_rehash(stored):
        # The one moment the password is in hand and re-hashing costs nothing
        # extra. Best effort, and deliberately not a reason to refuse a sign-in
        # that has already succeeded.
        user_id = (await query_user_by_email(db, address)).id
        await set_user_password(
            db, user_id, await run_in_threadpool(hash_password, password)
        )
        await db.commit()

    return await _complete_sign_in(
        request, db, address, next_path, provider="password"
    )


@router.post("/auth/password/register")
async def post_auth_password_register(
    request: Request,
    db: DBDep,
    email: str = Form(""),
    password: str = Form(""),
    confirm: str = Form(""),
    next: str = Form("/"),
):
    """Claim an address and give it a password.

    Specific sentences here, unlike `post_auth_password` above, and the
    difference is not an oversight: this is a form somebody is filling in
    rather than a credential somebody is presenting, so telling them their
    password is too short helps them and tells an attacker nothing they could
    not have learned by trying it on their own address.

    The domain check, the account and the blacklist all happen inside
    `_complete_sign_in`, in that order, which is what stops this route being a
    second opinion about any of them - and what guarantees an address outside
    `allowed_domains` never becomes a row.
    """
    _require_password_login()

    next_path = safe_next_path(next)
    try:
        address = normalize_email(email)
        check_password(password)
    except ValueError as err:
        return _login_page(
            request, next_path=next_path, error=str(err), status_code=400
        )

    if password != confirm:
        return _login_page(
            request,
            next_path=next_path,
            error="Those two passwords are not the same.",
            status_code=400,
        )

    return await _complete_sign_in(
        request,
        db,
        address,
        next_path,
        provider="password",
        claim_password=await run_in_threadpool(hash_password, password),
    )


@router.get("/auth/{provider}")
async def get_auth_provider(request: Request, provider: str, next: str = "/"):
    """Hand off to the provider.

    Authlib puts the `state` - and, for an OIDC provider, the `nonce` and the
    PKCE verifier - in the session and returns the redirect to the consent
    screen. One route for both providers rather than one each: everything here
    except the `hd` hint is the same sentence twice, and two copies of it would
    be two things to keep in step.
    """
    descriptor = _oauth_provider(provider)
    next_path = safe_next_path(next)
    client = oauth_client(provider)
    if client is None:
        return _login_page(
            request,
            next_path=next_path,
            error=f"{descriptor.label} sign-in is not configured on this server.",
            status_code=503,
        )

    # The destination rides in the session, not in the redirect URI: that URI is
    # matched character for character against the registration at the provider,
    # so it cannot carry a query string. Unlike an emailed link this is the same
    # browser throughout, which makes the session the natural place for it.
    request.session["login_next"] = next_path
    _prune_stale_oauth_state(request)

    params = {}
    google = core.CONFIG.auth.google
    if provider == "google" and google is not None and google.hd:
        # A hint for the account chooser, never a check: `hd` is absent for
        # consumer accounts, and the gate is `is_allowed_email` on the address.
        # Google's alone - GitHub has no notion of an organisation domain to
        # pre-select, which is why this is the one branch on the name here.
        params["hd"] = google.hd

    try:
        return await client.authorize_redirect(
            request, oauth_redirect_uri(_base_url(request), provider), **params
        )
    except Exception:
        # Typically the metadata document could not be fetched. A machine that
        # is offline should say so rather than raise a 500.
        logger.exception("could not start the %s sign-in flow", descriptor.label)
        return _login_page(
            request,
            next_path=next_path,
            error=f"Could not reach {descriptor.label} right now. Please try again.",
            status_code=502,
        )


@router.get("/auth/{provider}/callback")
async def get_auth_provider_callback(request: Request, provider: str, db: DBDep):
    """Finish the flow: get the address the provider vouches for, check the
    domain, sign in.

    Every failure lands on the login page with one sentence, never a traceback:
    the visitor arrives here from the provider, so a 500 would be a dead end.
    """
    descriptor = _oauth_provider(provider)
    # Read before `_sign_in` clears the session. A callback that arrives
    # without one - a bookmarked URL, a cleared cookie - lands on the root.
    next_path = safe_next_path(request.session.get("login_next"))

    client = oauth_client(provider)
    if client is None:
        return _login_page(
            request,
            error=f"{descriptor.label} sign-in is not configured on this server.",
            status_code=503,
        )

    try:
        token = await client.authorize_access_token(request)
    except OAuthError as err:
        # Covers the visitor pressing Cancel (`access_denied`), a mismatched or
        # expired `state`, and a token endpoint that refused the code. Keep the
        # clause this narrow: widening it would fold a failed state check into
        # a success path, which is the one bug here that fails silently.
        logger.info(
            "a %s sign-in did not complete: %s",
            descriptor.label,
            err.error,
            extra={"provider": descriptor.name, "reason": err.error},
        )
        return _login_page(
            request,
            next_path=next_path,
            error="That sign-in did not complete. Please try again.",
            status_code=400,
        )

    # The one thing the two providers do differently, and the only reason there
    # is a registry at all: Google hands the address over in a verified ID
    # token, GitHub has to be asked for it.
    try:
        email = await descriptor.email(client, token)
    except _NoProvedAddress as refusal:
        return _login_page(
            request,
            next_path=next_path,
            error=refusal.message,
            status_code=refusal.status_code,
        )

    # The domain, the account and the blacklist, shared with the sign-in box.
    return await _complete_sign_in(
        request, db, email, next_path, provider=descriptor.name
    )


@router.post("/logout")
async def post_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
