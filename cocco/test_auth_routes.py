"""End-to-end tests of the auth *and* authorization routes, over an in-memory
database.

Importing `cocco.main` mounts `cocco/static/` and reads `cocco/templates/`,
both resolved against the package rather than the working directory, so these
run from anywhere - and they need no database and never connect to one:
building an engine does not
connect, and `get_db` is overridden with a SQLite connection, which is enough
because the auth routes use plain Core only. Google is replaced wholesale by
`_FakeGoogleClient`, so nothing here reaches the network either.

The authorization tests at the bottom run here for the same reason, and it is
not a coincidence: a refusal happens in a *dependency*, so the route body - and
with it every Postgres-only page query - never runs. Which means the interesting
half, "who is turned away", is a unit test.
"""

from dataclasses import replace

import pytest
from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.starlette_client import OAuthError
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.auth import get_or_create_user
from cocco.config import GoogleConfig
from cocco.model import (
    Administrator,
    Challenge,
    Course,
    Enrollment,
    UserRole,
    metadata,
)
from cocco.repository import (
    block_user,
    query_course_role,
    query_enrollment_key,
    query_user_by_email,
)

# A signed-in visitor asking for something that does not need Postgres. Most
# pages run `json_agg` queries that only Postgres has, so this avatar route -
# a plain SELECT, and behind `CurrentUser` - is the canary for "am I signed
# in?" throughout this file.
BEHIND_THE_WALL = "/users/1/avatar"

VERIFIED = {"email": "dave@unipd.it", "email_verified": True}


@pytest.fixture
async def app_engine():
    """An empty database with the schema on it.

    The tables are created here rather than in `client` because not every test
    needs a client: the authorization dependencies are worth calling directly,
    and they still need somewhere to look a role up.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine


@pytest.fixture
async def client(app_engine, monkeypatch):
    """A client whose app talks to `app_engine` and never reaches Google."""
    import cocco.main as main

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
    """The check is on both branches of `get_user`: otherwise dummy auth would
    be the one configuration in which the block does not exist, which is the
    configuration it gets developed in."""
    _set_dummy_auth(monkeypatch, True, "alice@unipd.it")
    client.cookies.clear()
    # The first request creates the account.
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 200

    await _block(app_engine, "alice@unipd.it")
    assert client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 403


########################################################################
# dummy auth


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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "alice@unipd.it")

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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")

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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
    url = f"/courses/{a_locked_course}/enroll"

    response = client.post(url, data={"key": attempt, "next": "/courses/1"})
    assert response.status_code == 200
    assert "That key is not right" in response.text
    assert await _role_of(app_engine, "dave@unipd.it", a_locked_course) is None


async def test_the_right_enrollment_key_enrolls(
    client, monkeypatch, no_google, a_locked_course, app_engine
):
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")
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
    _set_dummy_auth(monkeypatch, True, "dave@unipd.it")

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
    _set_dummy_auth(monkeypatch, True, "alice@unipd.it")
    url = f"/courses/{ours['course_id']}/edit"
    saved = {"title": "Algorithms 101", "description": "About it."}

    client.post(url, data=saved | {"enrollment_key": f"  {THE_KEY}  "})
    async with app_engine.connect() as conn:
        assert await query_enrollment_key(conn, ours["course_id"]) == THE_KEY

    client.post(url, data=saved | {"enrollment_key": "   "})
    async with app_engine.connect() as conn:
        assert await query_enrollment_key(conn, ours["course_id"]) is None
