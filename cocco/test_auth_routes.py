"""End-to-end tests of the auth *and* authorization routes.

Both providers are replaced wholesale by `_FakeOAuthClient`, so nothing here
reaches the network: `authorize_redirect` is what a browser would follow,
`authorize_access_token` returns the claims as Authlib would have verified them
(or raises the way a cancelled or tampered flow does), and `get` is the whole of
GitHub's address API. That fake is the point of `cocco/auth.py` being the half
of sign-in with no network in it.

The authorization tests at the bottom sit in this file rather than beside the
pages they guard, and that is not filing: a refusal happens in a *dependency*,
so the route body never runs and what is being tested is who is turned away
rather than what they would have seen.

This file has no seeded cast. It *is* the account-creation test - the first
accepted address is what makes a user - so each fixture below builds only what
its own tests need.
"""

from dataclasses import replace

import pytest
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.starlette_client import OAuthError
from fastapi.responses import RedirectResponse
from sqlalchemy import insert

from cocco.conftest import PUBLISHED
from cocco.auth import get_or_create_user
from cocco.config import GithubConfig, GoogleConfig
from cocco.model import (
    Administrator,
    Challenge,
    Course,
    Enrollment,
    UserRole,
)
from cocco.repository._rest import (
    block_user,
    query_course_role,
    query_enrollment_key,
    query_user_by_email,
)

# A signed-in visitor asking for the cheapest thing there is: an avatar is a
# plain SELECT behind `CurrentUser`, so it answers "am I signed in?" without
# dragging a whole page in behind it. The id is 1 because the tables are emptied
# with `RESTART IDENTITY` between tests (see `conftest.db`), so the first
# account created is always the first row.
BEHIND_THE_WALL = "/users/1/avatar"

VERIFIED = {"email": "dave@unipd.it", "email_verified": True}

# GitHub's `/user/emails`, the shape the API actually returns: an account holds
# several addresses and only the verified ones prove anything. The common case
# is exactly this one - a personal primary with the institutional address
# beside it as a verified secondary.
GITHUB_EMAILS = [
    {"email": "dave@personal.test", "primary": True, "verified": True},
    {"email": "dave@unipd.it", "primary": False, "verified": True},
]


@pytest.fixture
def client(make_client):
    """A client whose app never reaches a provider, and whose development
    sign-in box is off: the shortcut bypasses everything these tests are
    about."""
    return make_client(dummy_auth=False)


class _FakeResponse:
    """What `client.get` hands back: the two things `_github_email` uses."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeOAuthClient:
    """Stands in for Authlib's registered client, for either provider.

    These are the only methods the routes call, so the whole of a provider is
    these few lines: `authorize_redirect` is what a browser would follow,
    `authorize_access_token` returns the claims as Authlib would have verified
    them - or raises, the way a cancelled or tampered flow does - and `get` is
    GitHub's address API, which Google's flow never touches.
    """

    def __init__(self, claims=None, error=None, emails=None):
        self.claims = claims
        self.error = error
        self.emails = emails
        self.redirects = []

    async def authorize_redirect(self, request, redirect_uri, **params):
        self.redirects.append((redirect_uri, params))
        return RedirectResponse(
            f"https://provider.test/o/oauth2/v2/auth?redirect_uri={redirect_uri}",
            status_code=302,
        )

    async def authorize_access_token(self, request):
        if self.error is not None:
            raise self.error
        return {"userinfo": self.claims}

    async def get(self, path, token=None):
        assert path == "user/emails"
        if isinstance(self.emails, Exception):
            raise self.emails
        return _FakeResponse(self.emails)


def _install_client(monkeypatch, provider: str, client):
    """Point the app's one client seam at a fake, for one provider only.

    `oauth_client` takes the name, so a test that configures Google must still
    get None for GitHub - otherwise the login page would grow a button for a
    provider the test never set up.
    """
    import cocco.main as main

    monkeypatch.setattr(
        main, "oauth_client", lambda name: client if name == provider else None
    )


@pytest.fixture
def google(monkeypatch):
    """Install a fake client and make the app believe it is configured.

    `dataclasses.replace` on the frozen config is what makes "configured" and
    "unconfigured" a one-line toggle without touching the filesystem.
    """
    import cocco.main as main

    def _install(claims=None, error=None, hd="unipd.it"):
        client = _FakeOAuthClient(claims, error)
        _install_client(monkeypatch, "google", client)
        monkeypatch.setattr(
            main,
            "CONFIG",
            replace(
                main.CONFIG,
                auth=replace(
                    main.CONFIG.auth,
                    google=GoogleConfig("an-id", "a-secret", hd=hd),
                    github=None,
                ),
            ),
        )
        return client

    return _install


@pytest.fixture
def github(monkeypatch):
    """The same toggle for GitHub, whose credentials are the whole of its
    configuration - there is no `hd` and no metadata document."""
    import cocco.main as main

    def _install(emails=None, error=None):
        client = _FakeOAuthClient(error=error, emails=emails)
        _install_client(monkeypatch, "github", client)
        monkeypatch.setattr(
            main,
            "CONFIG",
            replace(
                main.CONFIG,
                auth=replace(
                    main.CONFIG.auth,
                    google=None,
                    github=GithubConfig("an-id", "a-secret"),
                ),
            ),
        )
        return client

    return _install


@pytest.fixture
def no_google(monkeypatch):
    """A server with no credentials at all, which must still serve pages."""
    import cocco.main as main

    monkeypatch.setattr(main, "oauth_client", lambda name: None)
    monkeypatch.setattr(
        main,
        "CONFIG",
        replace(main.CONFIG, auth=replace(main.CONFIG.auth, google=None, github=None)),
    )


def _set_dummy_auth(client, monkeypatch, enabled: bool, user: str | None = None):
    """Turn the development shortcut on, and optionally walk through it.

    Two steps rather than one now, and the split is the feature: `dummy_auth`
    only puts a box on the login page, and being signed in is a request through
    it. So the tests below that assert *who* somebody is name a user here, and
    the ones about the box itself do not.
    """
    import cocco.main as main

    monkeypatch.setattr(main, "DUMMY_AUTH", enabled)
    if user is not None:
        response = client.post(
            "/auth/dummy", data={"email": user}, follow_redirects=False
        )
        assert response.status_code == 303, response.text


########################################################################
# the login page


async def test_the_login_page_is_public_and_offers_google(client, google):
    google()
    response = client.get("/login?next=/courses/1/2")
    assert response.status_code == 200
    assert "Sign in with Google" in response.text
    assert "/auth/google?next=%2Fcourses%2F1%2F2" in response.text


async def test_the_login_page_says_so_when_nothing_is_configured(client, no_google):
    """A checkout with no credentials must boot and explain itself, not 500.

    The warning is about the page having *no* way in rather than about Google
    in particular: with `[dev] dummy_auth` or `[auth] password_login` on there
    is a form here, and complaining about the absence of Google beside it would
    be a warning about a thing nobody is looking for."""
    response = client.get("/login")
    assert response.status_code == 200
    assert "No sign-in method is configured" in response.text
    assert "Sign in with Google" not in response.text


async def test_an_anonymous_page_request_is_redirected(client, google):
    google()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
    # ...and the visitor is sent back where they were headed
    response = client.get("/courses/1/2", follow_redirects=False)
    assert "next=%2Fcourses%2F1%2F2" in response.headers["location"]


async def test_an_anonymous_htmx_request_gets_hx_redirect(client, google):
    """htmx follows a 302 inside the XHR and would paint the login page into a
    polled fragment; the header makes the browser navigate instead."""
    google()
    response = client.get(
        "/f/leaderboard/1",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 204
    assert "/login" in response.headers["HX-Redirect"]
    assert response.text == ""


########################################################################
# starting the flow


async def test_starting_the_flow_redirects_to_google(client, google):
    fake = google()
    response = client.get("/auth/google?next=/courses/1/2", follow_redirects=False)
    assert response.status_code == 302
    assert "provider.test" in response.headers["location"]

    [(redirect_uri, params)] = fake.redirects
    assert redirect_uri.endswith("/auth/google/callback")
    # The URI is matched character for character by Google, so the destination
    # cannot ride in it.
    assert "?" not in redirect_uri
    assert params == {"hd": "unipd.it"}


async def test_starting_the_flow_without_credentials_does_not_crash(client, no_google):
    response = client.get("/auth/google", follow_redirects=False)
    assert response.status_code == 503
    assert "not configured" in response.text


########################################################################
# the callback


async def test_a_first_sign_in_creates_the_account(client, google, app_engine):
    google(claims=VERIFIED)
    client.get("/auth/google", follow_redirects=False)

    response = client.get("/auth/google/callback?code=x&state=y", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"

    async with app_engine.connect() as conn:
        row = await query_user_by_email(conn, "dave@unipd.it")
    assert row is not None and row.nickname == "dave"


async def test_a_second_sign_in_reuses_the_account(client, google, app_engine):
    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x&state=y", follow_redirects=False)
    async with app_engine.connect() as conn:
        first = await query_user_by_email(conn, "dave@unipd.it")

    client.get("/auth/google/callback?code=x&state=y", follow_redirects=False)
    async with app_engine.connect() as conn:
        second = await query_user_by_email(conn, "dave@unipd.it")

    assert first is not None and second is not None
    assert first.id == second.id


async def test_a_subdomain_of_the_institution_is_accepted(client, google, app_engine):
    """Students are @studenti.unipd.it, which does not end in @unipd.it."""
    google(claims={"email": "Bob@Studenti.UNIPD.it", "email_verified": True})
    response = client.get("/auth/google/callback?code=x", follow_redirects=False)
    assert response.status_code == 303

    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "bob@studenti.unipd.it") is not None


async def test_an_address_outside_the_allowed_domains_is_refused(
    client, google, app_engine
):
    google(claims={"email": "eve@gmail.com", "email_verified": True})
    response = client.get("/auth/google/callback?code=x")
    assert response.status_code == 403
    assert "not an institutional address" in response.text

    # Nothing is created for an address that was never going to be admitted.
    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "eve@gmail.com") is None


@pytest.mark.parametrize("verified", [False, None, "false", 0, "no"])
async def test_an_unverified_address_is_refused(
    client, google, app_engine, verified
):
    """Google says the address is unconfirmed, so it proves nothing. The string
    spellings are here because `"false"` is truthy, which is the kind of bug
    that would never fail visibly."""
    google(claims={"email": "eve@unipd.it", "email_verified": verified})
    response = client.get("/auth/google/callback?code=x")
    assert response.status_code == 403
    assert "not verified" in response.text

    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "eve@unipd.it") is None


@pytest.mark.parametrize("verified", [True, "true"])
async def test_both_spellings_of_a_verified_address_are_accepted(
    client, google, verified
):
    google(claims={"email": "dave@unipd.it", "email_verified": verified})
    assert client.get(
        "/auth/google/callback?code=x", follow_redirects=False
    ).status_code == 303


async def test_claims_without_an_address_are_refused(client, google):
    google(claims={"email_verified": True})
    response = client.get("/auth/google/callback?code=x")
    assert response.status_code == 400
    assert "did not return an email address" in response.text


async def test_a_denied_consent_lands_on_the_login_page(client, google):
    google(error=OAuthError(error="access_denied"))
    response = client.get("/auth/google/callback?error=access_denied")
    assert response.status_code == 400
    assert "did not complete" in response.text


async def test_a_mismatched_state_lands_on_the_login_page(client, google):
    """The login-CSRF defence. Pinned here so the `except OAuthError` in the
    callback is never widened into something that swallows it."""
    google(error=MismatchingStateError())
    response = client.get("/auth/google/callback?code=x&state=forged")
    assert response.status_code == 400
    assert "did not complete" in response.text


async def test_a_callback_without_credentials_does_not_crash(client, no_google):
    response = client.get("/auth/google/callback?code=x")
    assert response.status_code == 503


########################################################################
# GitHub, which is the same flow with one different step: there is no ID token,
# so the address is asked for rather than handed over.


async def test_the_login_page_offers_github(client, github):
    github(emails=GITHUB_EMAILS)
    response = client.get("/login?next=/courses/1/2")
    assert response.status_code == 200
    assert "Sign in with GitHub" in response.text
    assert "/auth/github?next=%2Fcourses%2F1%2F2" in response.text
    # One button per *configured* provider: Google is off in this fixture.
    assert "Sign in with Google" not in response.text


async def test_starting_the_github_flow_sends_no_hd(client, github):
    """`hd` is Google's account-chooser hint and nothing else has one, so the
    one branch on the provider name in `get_auth_provider` is pinned here."""
    fake = github(emails=GITHUB_EMAILS)
    response = client.get("/auth/github?next=/courses/1/2", follow_redirects=False)
    assert response.status_code == 302

    [(redirect_uri, params)] = fake.redirects
    assert redirect_uri.endswith("/auth/github/callback")
    assert "?" not in redirect_uri
    assert params == {}


async def test_starting_the_github_flow_without_credentials_does_not_crash(
    client, no_google
):
    response = client.get("/auth/github", follow_redirects=False)
    assert response.status_code == 503
    assert "not configured" in response.text


@pytest.mark.parametrize("path", ["/auth/gitlab", "/auth/gitlab/callback"])
async def test_an_unknown_provider_is_a_404(client, github, path):
    """A path this app does not serve should not admit that it exists -
    `post_auth_dummy`'s rule, and the reason this is not a 422."""
    github(emails=GITHUB_EMAILS)
    assert client.get(path, follow_redirects=False).status_code == 404


async def test_github_signs_in_with_the_verified_institutional_address(
    client, github, app_engine
):
    """The primary is personal and the institutional address sits beside it as
    a verified secondary, which is the ordinary shape of a student's account.
    The one that passes `allowed_domains` is the one that signs them in."""
    github(emails=GITHUB_EMAILS)
    client.get("/auth/github", follow_redirects=False)

    response = client.get("/auth/github/callback?code=x&state=y", follow_redirects=False)
    assert response.status_code == 303

    async with app_engine.connect() as conn:
        row = await query_user_by_email(conn, "dave@unipd.it")
    assert row is not None and row.nickname == "dave"
    # And nothing was created for the address that did not admit them.
    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "dave@personal.test") is None


async def test_github_never_looks_at_an_unverified_address(client, github, app_engine):
    """Unverified proves nothing, so it is not a candidate at all - even when
    it is the only address inside the allowed domains."""
    github(
        emails=[
            {"email": "dave@personal.test", "primary": True, "verified": True},
            {"email": "eve@unipd.it", "primary": False, "verified": False},
        ]
    )
    response = client.get("/auth/github/callback?code=x")
    assert response.status_code == 403
    assert "not an institutional address" in response.text

    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "eve@unipd.it") is None


async def test_github_with_no_address_in_the_domains_names_the_domains(client, github):
    """The primary comes back for the refusal rather than a second, vaguer
    sentence of GitHub's own: `_complete_sign_in` names the domains, which is
    the message a person can act on."""
    github(emails=[{"email": "eve@gmail.com", "primary": True, "verified": True}])
    response = client.get("/auth/github/callback?code=x")
    assert response.status_code == 403
    assert "not an institutional address" in response.text


async def test_github_with_no_verified_address_at_all_is_refused(client, github):
    github(emails=[{"email": "eve@unipd.it", "primary": True, "verified": False}])
    response = client.get("/auth/github/callback?code=x")
    assert response.status_code == 403
    assert "no verified email address" in response.text


async def test_a_github_api_failure_lands_on_the_login_page(client, github):
    """The one network call signing in makes. A provider that is down is a
    sentence, never a traceback: the visitor arrived here from GitHub."""
    github(emails=RuntimeError("boom"))
    response = client.get("/auth/github/callback?code=x")
    assert response.status_code == 502
    assert "Could not reach GitHub" in response.text


async def test_a_denied_github_consent_lands_on_the_login_page(client, github):
    github(error=OAuthError(error="access_denied"))
    response = client.get("/auth/github/callback?error=access_denied")
    assert response.status_code == 400
    assert "did not complete" in response.text


async def test_a_github_callback_without_credentials_does_not_crash(client, no_google):
    response = client.get("/auth/github/callback?code=x")
    assert response.status_code == 503


########################################################################
# where the visitor lands


async def test_the_next_destination_survives_the_round_trip(client, google):
    google(claims=VERIFIED)
    client.get("/auth/google?next=/courses/1/2", follow_redirects=False)
    response = client.get("/auth/google/callback?code=x", follow_redirects=False)
    assert response.headers["location"] == "/courses/1/2"


async def test_a_hostile_next_is_filtered(client, google):
    google(claims=VERIFIED)
    client.get("/auth/google?next=https://evil.example", follow_redirects=False)
    response = client.get("/auth/google/callback?code=x", follow_redirects=False)
    assert response.headers["location"] == "/"


async def test_a_callback_with_no_session_lands_on_the_root(client, google):
    google(claims=VERIFIED)
    response = client.get("/auth/google/callback?code=x", follow_redirects=False)
    assert response.headers["location"] == "/"


########################################################################
# the session


async def test_signing_in_replaces_any_prior_session(client, google):
    """Session fixation: whatever the cookie held before, it holds only the new
    identity afterwards."""
    import cocco.main as main

    google(claims=VERIFIED)
    client.get("/auth/google?next=/courses/9", follow_redirects=False)
    client.get("/auth/google/callback?code=x", follow_redirects=False)

    cookie = client.cookies.get(main.CONFIG.session.cookie_name)
    assert cookie
    # `login_next` and Authlib's spent state are gone with the rest of it.
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code != 303


async def test_logout_clears_the_session(client, google):
    import cocco.main as main

    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)
    assert client.cookies.get(main.CONFIG.session.cookie_name)

    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 303


async def test_a_session_pointing_at_nobody_is_discarded(client, google, app_engine):
    """The cookie holds only a user id, so a `[dev] reset_db` under a live tab
    logs that tab out rather than blowing up."""
    from sqlalchemy import delete

    from cocco.model import User

    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)

    async with app_engine.connect() as conn:
        await conn.execute(delete(User).where(User.c.email == "dave@unipd.it"))
        await conn.commit()

    response = client.get(BEHIND_THE_WALL, follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]


########################################################################
# blocked accounts
#
# The block is on `user`, checked in `get_user` on the row it already reads, so
# it is re-asked on every request rather than only at the door. What these pin
# is the difference from the two refusals either side of it: it is not
# `NotAuthenticated` (which redirects to a login page that could not explain
# this) and it does not end with the session.


async def _block(app_engine, email: str, reason: str = "spamming the judge"):
    async with app_engine.connect() as conn:
        row = await query_user_by_email(conn, email)
        assert row is not None
        assert await block_user(conn, row.id, reason)
        await conn.commit()


async def test_a_block_takes_effect_on_the_next_request(client, google, app_engine):
    """Signed in a moment ago, shut out now - without touching their cookie."""
    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 200

    await _block(app_engine, "dave@unipd.it")

    response = client.get(BEHIND_THE_WALL, follow_redirects=False)
    assert response.status_code == 403
    assert "spamming the judge" in response.text


async def test_a_block_is_not_a_logout(client, google, app_engine):
    """The session is left alone: clearing it would send them to a login page
    that explains nothing, while the block page explains everything."""
    import cocco.main as main

    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)
    await _block(app_engine, "dave@unipd.it")

    for _ in range(2):
        assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 403
    assert client.cookies.get(main.CONFIG.session.cookie_name)


async def test_a_blocked_htmx_request_is_not_sent_to_the_login_page(
    client, google, app_engine
):
    """The mistake this code is one line away from: `handle_not_authenticated`
    answers htmx with 204 + HX-Redirect, and doing that here would conflate
    being blocked with being signed out."""
    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)
    await _block(app_engine, "dave@unipd.it")

    response = client.get(
        BEHIND_THE_WALL, headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert "HX-Redirect" not in response.headers


async def test_a_blocked_user_cannot_sign_in(client, google, app_engine):
    """Refused at the callback too, so the sentence arrives on the page they
    are already looking at rather than after a sign-in that dies on the first
    click."""
    import cocco.main as main

    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)
    await _block(app_engine, "dave@unipd.it")
    client.cookies.clear()

    response = client.get("/auth/google/callback?code=x", follow_redirects=False)
    assert response.status_code == 403
    assert "spamming the judge" in response.text
    # And no session came out of it.
    assert not client.cookies.get(main.CONFIG.session.cookie_name)
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 303


async def test_a_blocked_user_can_still_sign_out(client, google, app_engine):
    """The one control the block page offers them, and the way to stop an open
    tab from polling into 403s."""
    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)
    await _block(app_engine, "dave@unipd.it")

    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303


async def test_a_block_with_no_reason_still_says_something(
    client, google, app_engine
):
    """The reason is optional, so the page has to have a sentence of its own."""
    google(claims=VERIFIED)
    client.get("/auth/google/callback?code=x", follow_redirects=False)

    async with app_engine.connect() as conn:
        row = await query_user_by_email(conn, "dave@unipd.it")
        await block_user(conn, row.id, None)
        await conn.commit()

    response = client.get(BEHIND_THE_WALL, follow_redirects=False)
    assert response.status_code == 403
    assert "No reason was recorded" in response.text


async def test_dummy_auth_is_blocked_too(client, monkeypatch, app_engine, no_google):
    """A session minted by the box is a session like any other, so it meets the
    same `get_user`. There is only one branch there now, and this is what says
    so from the side dummy auth actually gets developed in."""
    _set_dummy_auth(client, monkeypatch, True, "alice@unipd.it")
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 200

    await _block(app_engine, "alice@unipd.it")
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 403


########################################################################
# dummy auth: the development sign-in box


async def test_the_login_page_offers_the_box_only_under_dummy_auth(
    client, monkeypatch, no_google
):
    import cocco.main as main

    monkeypatch.setattr(main, "DUMMY_AUTH", False)
    assert "/auth/dummy" not in client.get("/login").text

    monkeypatch.setattr(main, "DUMMY_AUTH", True)
    response = client.get("/login")
    assert response.status_code == 200
    assert "/auth/dummy" in response.text
    # The cast it names comes from `main`, so it cannot drift from the seed.
    assert main.DUMMY_ADMIN in response.text


async def test_the_box_signs_in_and_creates_the_account(
    client, monkeypatch, app_engine, no_google
):
    """It works against an empty database, creating the account on the way -
    with no enrollments, which is exactly what a first real sign-in gets."""
    _set_dummy_auth(client, monkeypatch, True)

    response = client.post(
        "/auth/dummy", data={"email": "alice@unipd.it"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 200

    async with app_engine.connect() as conn:
        row = await query_user_by_email(conn, "alice@unipd.it")
    assert row is not None and row.nickname == "alice"


async def test_the_box_is_the_only_identity(client, monkeypatch, app_engine, no_google):
    """Nobody is signed in until somebody types an address: `dummy_auth` on its
    own is a form, not a session, which is the whole change."""
    _set_dummy_auth(client, monkeypatch, True)
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 303

    _set_dummy_auth(client, monkeypatch, True, "bob@unipd.it")
    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "bob@unipd.it") is not None
        assert await query_user_by_email(conn, "alice@unipd.it") is None


async def test_the_box_takes_the_next_destination(client, monkeypatch, no_google):
    _set_dummy_auth(client, monkeypatch, True)
    response = client.post(
        "/auth/dummy",
        data={"email": "alice@unipd.it", "next": "/courses/1/2"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/courses/1/2"


async def test_a_hostile_next_on_the_box_is_filtered(client, monkeypatch, no_google):
    """`safe_next_path`, like every other destination in the app."""
    _set_dummy_auth(client, monkeypatch, True)
    response = client.post(
        "/auth/dummy",
        data={"email": "alice@unipd.it", "next": "https://evil.test/phish"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


async def test_the_box_refuses_an_address_outside_the_allowed_domains(
    client, monkeypatch, app_engine, no_google
):
    """The gate the Google callback applies, applied here too - `dummy_auth`
    skips proving the address, not the rules about which addresses there are."""
    _set_dummy_auth(client, monkeypatch, True)
    response = client.post(
        "/auth/dummy", data={"email": "someone@example.test"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert "not an institutional address" in response.text

    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "someone@example.test") is None


async def test_the_box_refuses_an_empty_address(client, monkeypatch, no_google):
    """A defaulted `Form` field rather than a required one: a 422 would be a
    page with nothing on it for the person looking at the box."""
    _set_dummy_auth(client, monkeypatch, True)
    response = client.post("/auth/dummy", data={}, follow_redirects=False)
    assert response.status_code == 400
    assert "email address is required" in response.text


async def test_the_box_refuses_a_blocked_account(
    client, monkeypatch, app_engine, no_google
):
    """Refused at the door rather than on the first click, which is the reason
    the Google callback checks it too - and now the one reason, once."""
    _set_dummy_auth(client, monkeypatch, True, "alice@unipd.it")
    await _block(app_engine, "alice@unipd.it")

    response = client.post(
        "/auth/dummy", data={"email": "alice@unipd.it"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert "This account is blocked" in response.text
    assert "spamming the judge" in response.text


async def test_the_box_is_not_there_at_all_without_dummy_auth(client, monkeypatch):
    """A hidden form is presentation and never the check: the route itself
    refuses, and with a 404, so a deployment does not admit it exists."""
    import cocco.main as main

    monkeypatch.setattr(main, "DUMMY_AUTH", False)
    response = client.post(
        "/auth/dummy", data={"email": "alice@unipd.it"}, follow_redirects=False
    )
    assert response.status_code == 404


########################################################################
# authorization: enrollment is the gate on a course's contents


@pytest.fixture
async def a_course(app_engine):
    """Two courses, one challenge each, and nobody enrolled on either.

    Who is on which course is the whole subject of these tests, so no fixture
    decides it. The second course exists to be the one the viewer is *not* on,
    which is what makes the cross-course checks mean anything.
    """
    ours, theirs = {}, {}
    async with app_engine.begin() as conn:
        for target, title in ((ours, "Algorithms 101"), (theirs, "Systems")):
            target["course_id"] = (
                await conn.execute(
                    insert(Course)
                    .values(title=title, description=f"About {title}.")
                    .returning(Course.c.id)
                )
            ).scalar_one()
            target["challenge_id"] = (
                await conn.execute(
                    insert(Challenge)
                    .values(
                        title=f"{title} - Shortest Route Planner",
                        description="Find the shortest route.",
                        course_id=target["course_id"],
                        max_memory_bytes=1 << 20,
                        max_time_s=1.0,
                        max_threads=1,
                        visible_from=PUBLISHED,
                    )
                    .returning(Challenge.c.id)
                )
            ).scalar_one()
    return ours, theirs


async def _enroll(app_engine, email: str, course_id: int, role: UserRole):
    """Sign `email` up for a course, creating the account if it is new."""
    async with app_engine.begin() as conn:
        user = await get_or_create_user(conn, email)
        await conn.execute(
            insert(Enrollment).values(
                user_id=user.id, course_id=course_id, role=role
            )
        )
    return user


def _gated_urls(course_id: int, challenge_id: int):
    """Every route a course's contents hide behind, as (method, url)."""
    return [
        ("get", f"/courses/{course_id}/{challenge_id}"),
        ("post", f"/courses/{course_id}/{challenge_id}/submissions"),
        ("get", f"/f/submissions/{challenge_id}"),
        ("get", f"/f/leaderboard/{challenge_id}"),
        ("get", f"/challenges/{challenge_id}/samples/1/input"),
    ]


async def test_a_non_member_is_refused_a_courses_contents(
    client, monkeypatch, no_google, a_course
):
    """The gate itself: signed in is not enough, enrollment is the check."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    ours, _ = a_course

    for method, url in _gated_urls(ours["course_id"], ours["challenge_id"]):
        response = getattr(client, method)(url)
        assert response.status_code == 403, url
        assert "not enrolled" in response.text, url


async def test_staff_of_one_course_are_not_staff_of_another(
    client, monkeypatch, no_google, a_course, app_engine
):
    """`role` lives on `enrollment` and is per course, so a teacher is a
    stranger everywhere they were not enrolled."""
    ours, theirs = a_course
    await _enroll(app_engine, "alice@unipd.it", ours["course_id"], UserRole.teacher)
    _set_dummy_auth(client, monkeypatch, True, "alice@unipd.it")

    for method, url in _gated_urls(theirs["course_id"], theirs["challenge_id"]):
        assert getattr(client, method)(url).status_code == 403, url


async def test_a_member_cannot_reach_another_courses_challenge_through_their_own(
    client, monkeypatch, no_google, a_course, app_engine
):
    """The reason `require_challenge_viewer` re-checks `challenge_in_course`:
    the role comes from the `course_id` in the path, so naming a course one is
    enrolled on in front of somebody else's challenge must not work. The pair
    is a URL for a challenge that does not exist under that course - 404."""
    ours, theirs = a_course
    await _enroll(app_engine, "dave@unipd.it", ours["course_id"], UserRole.student)
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")

    smuggled = f"/courses/{ours['course_id']}/{theirs['challenge_id']}"
    assert client.get(smuggled).status_code == 404


async def test_the_gate_admits_every_role(app_engine, a_course):
    """Called directly, because the far side of the gate is a page query only
    Postgres can run. `EDITOR_ROLES` is a subset of "enrolled", so staff pass
    the membership check without a second one."""
    from cocco.main import Forbidden, require_challenge_member, require_course_member

    ours, _ = a_course
    for i, role in enumerate(UserRole):
        user = await _enroll(
            app_engine, f"member{i}@unipd.it", ours["course_id"], role
        )
        async with app_engine.connect() as conn:
            assert await require_course_member(ours["course_id"], conn, user) == role
            assert (
                await require_challenge_member(ours["challenge_id"], conn, user)
                == role
            )

    stranger = await _enroll(
        app_engine, "erin@unipd.it", a_course[1]["course_id"], UserRole.student
    )
    async with app_engine.connect() as conn:
        with pytest.raises(Forbidden):
            await require_course_member(ours["course_id"], conn, stranger)
        with pytest.raises(Forbidden):
            await require_challenge_member(ours["challenge_id"], conn, stranger)


async def test_an_administrator_is_inside_every_gate(app_engine, a_course):
    """A site administrator has no enrollment at all and still passes, because
    `effective_course_role` answers `teacher` for them - they are course staff
    everywhere. Called directly, for the reason at the top of this file: the far
    side of the gate is a page query only Postgres can run.
    """
    from cocco.main import (
        require_challenge_member,
        require_course_editor,
        require_course_member,
    )

    ours, _ = a_course
    async with app_engine.begin() as conn:
        admin = await get_or_create_user(conn, "root@unipd.it")
        await conn.execute(insert(Administrator).values(email=admin.email))

    async with app_engine.connect() as conn:
        assert (
            await require_course_member(ours["course_id"], conn, admin)
            == UserRole.teacher
        )
        assert (
            await require_course_editor(ours["course_id"], conn, admin)
            == UserRole.teacher
        )
        assert (
            await require_challenge_member(ours["challenge_id"], conn, admin)
            == UserRole.teacher
        )


async def test_the_course_page_previews_for_a_non_member(
    client, monkeypatch, no_google, a_course
):
    """The one deliberate hole in the gate: the course page is where a link
    from the home page lands and where the way in lives, so it shows the title,
    the description and `Enroll!` - and nothing of the contents."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    ours, _ = a_course

    response = client.get(f"/courses/{ours['course_id']}")
    assert response.status_code == 200
    assert "Algorithms 101" in response.text
    assert "About Algorithms 101." in response.text
    assert "not enrolled" in response.text
    assert "Enroll!" in response.text
    # The challenge list is not merely hidden by the template: `get_course`
    # never ran the query that would have fetched it.
    assert "Shortest Route Planner" not in response.text


async def test_enrolling_from_the_preview_comes_back_to_the_course(
    client, monkeypatch, no_google, a_course, app_engine
):
    """`next` is what turns the preview into the course page, and it is
    filtered like every other destination in the app."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    ours, _ = a_course
    course_url = f"/courses/{ours['course_id']}"

    # The preview offers it, so the preview is where it is posted from
    assert f'value="{course_url}"' in client.get(course_url).text

    response = client.post(f"{course_url}/enroll", data={"next": course_url})
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == course_url

    # ...and the row that opens the gate is there. What is behind the gate is
    # a `json_agg` page query, so `test_the_gate_admits_every_role` is where
    # the opening itself is checked.
    async with app_engine.connect() as conn:
        dave = await query_user_by_email(conn, "dave@unipd.it")
        assert (
            await query_course_role(
                conn, user_id=dave.id, course_id=ours["course_id"]
            )
            == UserRole.student
        )


async def test_a_hostile_next_on_enrolling_is_filtered(
    client, monkeypatch, no_google, a_course
):
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    ours, _ = a_course

    response = client.post(
        f"/courses/{ours['course_id']}/enroll",
        data={"next": "https://evil.test/phish"},
    )
    assert response.headers["HX-Redirect"] == "/"


async def test_a_refusal_is_a_page_not_json(
    client, monkeypatch, no_google, a_course
):
    """These are URLs an ordinary visitor can now reach by clicking, so the
    answer has to be readable."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    ours, _ = a_course

    response = client.get(f"/courses/{ours['course_id']}/{ours['challenge_id']}")
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    # Rendered through `base.jinja`, with the signed-in navbar and a way out
    assert "dave@unipd.it" in response.text
    assert "Back to the courses" in response.text


########################################################################
# the enrollment key: an optional shared secret on the way in

THE_KEY = "Sesame-2026"


@pytest.fixture
async def a_locked_course(app_engine):
    """A course nobody is on, that wants a key."""
    async with app_engine.begin() as conn:
        course_id = (
            await conn.execute(
                insert(Course)
                .values(
                    title="Compilers",
                    description="Lexing, parsing, code generation.",
                    enrollment_key=THE_KEY,
                )
                .returning(Course.c.id)
            )
        ).scalar_one()
    return course_id


async def _role_of(app_engine, email: str, course_id: int):
    async with app_engine.connect() as conn:
        user = await query_user_by_email(conn, email)
        if user is None:
            return None
        return await query_course_role(conn, user_id=user.id, course_id=course_id)


@pytest.mark.parametrize(
    "attempt", ["", "sesame-2026", "Sesame-2025", "Sesame", "Sesame-2026x"]
)
async def test_a_wrong_enrollment_key_enrolls_nobody(
    client, monkeypatch, no_google, a_locked_course, app_engine, attempt
):
    """Including the empty one, and including a differently cased one: the key
    is a string the teacher chose, so deciding that case does not count is not
    ours to make."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    url = f"/courses/{a_locked_course}/enroll"

    response = client.post(url, data={"key": attempt, "next": "/courses/1"})
    assert response.status_code == 200
    assert "That key is not right" in response.text
    assert await _role_of(app_engine, "dave@unipd.it", a_locked_course) is None


async def test_the_right_enrollment_key_enrolls(
    client, monkeypatch, no_google, a_locked_course, app_engine
):
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    course_url = f"/courses/{a_locked_course}"

    response = client.post(
        f"{course_url}/enroll", data={"key": THE_KEY, "next": course_url}
    )
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == course_url
    assert (
        await _role_of(app_engine, "dave@unipd.it", a_locked_course)
        == UserRole.student
    )


async def test_a_pasted_enrollment_key_is_trimmed(
    client, monkeypatch, no_google, a_locked_course, app_engine
):
    """A key read off a slide arrives with whitespace around it, and that is
    not the student getting it wrong."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    course_url = f"/courses/{a_locked_course}"

    response = client.post(
        f"{course_url}/enroll", data={"key": f"  {THE_KEY}\n", "next": course_url}
    )
    assert response.status_code == 204
    assert (
        await _role_of(app_engine, "dave@unipd.it", a_locked_course)
        == UserRole.student
    )


async def test_a_course_without_a_key_asks_for_none(
    client, monkeypatch, no_google, a_course, app_engine
):
    """The open path is untouched: no `key` field is sent at all, and nothing
    demands one. Posted with `next`, as the preview does, because the home
    page's answer is the courses fragment and that is a `json_agg` query."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")
    ours, _ = a_course
    course_url = f"/courses/{ours['course_id']}"

    response = client.post(f"{course_url}/enroll", data={"next": course_url})
    assert response.status_code == 204
    assert (
        await _role_of(app_engine, "dave@unipd.it", ours["course_id"])
        == UserRole.student
    )


async def test_the_preview_asks_for_the_key_and_never_shows_it(
    client, monkeypatch, no_google, a_locked_course
):
    """The page is served to people who have not proved they know the key, so
    `query_course` reports `requires_key` and nothing more."""
    _set_dummy_auth(client, monkeypatch, True, "dave@unipd.it")

    response = client.get(f"/courses/{a_locked_course}")
    assert response.status_code == 200
    assert "This course needs an enrollment key" in response.text
    assert 'name="key"' in response.text
    assert THE_KEY not in response.text


async def test_the_editor_saves_and_clears_the_key(
    client, monkeypatch, no_google, a_course, app_engine
):
    """Staff set the key from the course editor, and emptying the box reopens
    the course rather than setting a key nobody can type."""
    ours, _ = a_course
    await _enroll(app_engine, "alice@unipd.it", ours["course_id"], UserRole.teacher)
    _set_dummy_auth(client, monkeypatch, True, "alice@unipd.it")
    url = f"/courses/{ours['course_id']}/edit"
    saved = {"title": "Algorithms 101", "description": "About it."}

    client.post(url, data=saved | {"enrollment_key": f"  {THE_KEY}  "})
    async with app_engine.connect() as conn:
        assert await query_enrollment_key(conn, ours["course_id"]) == THE_KEY

    client.post(url, data=saved | {"enrollment_key": "   "})
    async with app_engine.connect() as conn:
        assert await query_enrollment_key(conn, ours["course_id"]) is None
