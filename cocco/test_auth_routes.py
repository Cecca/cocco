"""End-to-end tests of the auth routes, over an in-memory database.

Importing `cocco.main` reads PGUSER/PGDATABASE/PGHOST and mounts `static/`, so
these run from the repository root inside the devShell - but they never connect
to Postgres: `get_db` is overridden with a SQLite connection, which is enough
because the auth routes use plain Core only. Google is replaced wholesale by
`_FakeGoogleClient`, so nothing here reaches the network either.
"""

from dataclasses import replace

import pytest
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.starlette_client import OAuthError
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.auth import GoogleConfig
from cocco.model import metadata
from cocco.repository import query_user_by_email

# A signed-in visitor asking for something that does not need Postgres. Most
# pages run `json_agg` queries that only Postgres has, so this avatar route -
# a plain SELECT, and behind `CurrentUser` - is the canary for "am I signed
# in?" throughout this file.
BEHIND_THE_WALL = "/users/1/avatar"

VERIFIED = {"email": "dave@unipd.it", "email_verified": True}


@pytest.fixture
def app_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    yield engine


@pytest.fixture
async def client(app_engine, monkeypatch):
    """A client whose app talks to `app_engine` and never reaches Google."""
    import cocco.main as main

    async with app_engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    async def _get_db():
        async with app_engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db
    # Off unless a test says otherwise: the dummy shortcut bypasses everything
    # these tests are about.
    monkeypatch.setattr(main, "DUMMY_AUTH", False)

    # Deliberately not `with TestClient(...)`: entering the context runs the
    # startup event, which calls `create_db_and_tables` against the real
    # Postgres engine. Nothing here needs it, and these stay unit tests.
    client = TestClient(main.app)
    yield client
    main.app.dependency_overrides.clear()
    await app_engine.dispose()


class _FakeGoogleClient:
    """Stands in for Authlib's registered client.

    These are the only two methods the routes call, so the whole of Google is
    these few lines: `authorize_redirect` is what a browser would follow, and
    `authorize_access_token` returns the claims as Authlib would have verified
    them - or raises, the way a cancelled or tampered flow does.
    """

    def __init__(self, claims=None, error=None):
        self.claims = claims
        self.error = error
        self.redirects = []

    async def authorize_redirect(self, request, redirect_uri, **params):
        self.redirects.append((redirect_uri, params))
        return RedirectResponse(
            f"https://accounts.google.test/o/oauth2/v2/auth?redirect_uri={redirect_uri}",
            status_code=302,
        )

    async def authorize_access_token(self, request):
        if self.error is not None:
            raise self.error
        return {"userinfo": self.claims}


@pytest.fixture
def google(monkeypatch):
    """Install a fake client and make the app believe it is configured.

    `dataclasses.replace` on the frozen config is what makes "configured" and
    "unconfigured" a one-line toggle without touching the filesystem.
    """
    import cocco.main as main

    def _install(claims=None, error=None, hd="unipd.it"):
        client = _FakeGoogleClient(claims, error)
        monkeypatch.setattr(main, "google_client", lambda: client)
        monkeypatch.setattr(
            main,
            "CONFIG",
            replace(
                main.CONFIG,
                auth=replace(
                    main.CONFIG.auth,
                    google=GoogleConfig("an-id", "a-secret", hd=hd),
                ),
            ),
        )
        return client

    return _install


@pytest.fixture
def no_google(monkeypatch):
    """A server with no credentials at all, which must still serve pages."""
    import cocco.main as main

    monkeypatch.setattr(main, "google_client", lambda: None)
    monkeypatch.setattr(
        main,
        "CONFIG",
        replace(main.CONFIG, auth=replace(main.CONFIG.auth, google=None)),
    )


def _set_dummy_auth(monkeypatch, enabled: bool, user: str | None = None):
    import cocco.main as main

    monkeypatch.setattr(main, "DUMMY_AUTH", enabled)
    if user is not None:
        monkeypatch.setattr(main, "DUMMY_USER", user)


########################################################################
# the login page


async def test_the_login_page_is_public_and_offers_google(client, google):
    google()
    response = client.get("/login?next=/courses/1/2")
    assert response.status_code == 200
    assert "Sign in with Google" in response.text
    assert "/auth/google?next=%2Fcourses%2F1%2F2" in response.text


async def test_the_login_page_says_so_when_google_is_unconfigured(client, no_google):
    """A checkout with no credentials must boot and explain itself, not 500."""
    response = client.get("/login")
    assert response.status_code == 200
    assert "not configured" in response.text
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
    assert "accounts.google.test" in response.headers["location"]

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
    """The cookie holds only a user id, so a COCCO_RESET_DB under a live tab
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
# COCCO_DUMMY_AUTH


async def test_dummy_auth_signs_in_with_no_click(
    client, monkeypatch, app_engine, no_google
):
    """No cookie, no credentials, no login page - and it works against an empty
    database, creating the account on the way."""
    _set_dummy_auth(monkeypatch, True, "alice@unipd.it")
    client.cookies.clear()

    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code != 303
    async with app_engine.connect() as conn:
        row = await query_user_by_email(conn, "alice@unipd.it")
    assert row is not None and row.nickname == "alice"


async def test_dummy_auth_honours_cocco_dummy_user(
    client, monkeypatch, app_engine, no_google
):
    _set_dummy_auth(monkeypatch, True, "bob@unipd.it")
    client.get(BEHIND_THE_WALL, follow_redirects=False)

    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "bob@unipd.it") is not None
        assert await query_user_by_email(conn, "alice@unipd.it") is None


async def test_dummy_auth_skips_the_login_page(client, monkeypatch, no_google):
    _set_dummy_auth(monkeypatch, True)
    response = client.get("/login?next=/courses/1/2", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/courses/1/2"

    # And there is no flow to start either.
    response = client.get("/auth/google", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
