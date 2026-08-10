"""End-to-end tests of the four auth routes, over an in-memory database.

Importing `cocco.main` reads PGUSER/PGDATABASE/PGHOST and mounts `static/`, so
these run from the repository root inside the devShell - but they never connect
to Postgres: `get_db` is overridden with a SQLite connection, which is enough
because the auth routes use plain Core only.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.model import metadata
from cocco.repository import insert_login_token, query_user_by_email


@pytest.fixture
def app_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool
    )
    yield engine


@pytest.fixture
async def client(app_engine, monkeypatch):
    """A client whose app talks to `app_engine` and never sends mail."""
    import cocco.main as main

    async with app_engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

    async def _get_db():
        async with app_engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db
    # The mail that would otherwise be logged; capturing it is how a test gets
    # hold of a link without an inbox.
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main, "send_magic_link", lambda config, to, link: sent.append((to, link))
    )

    # Deliberately not `with TestClient(...)`: entering the context runs the
    # startup event, which calls `create_db_and_tables` against the real
    # Postgres engine. Nothing here needs it, and these stay unit tests.
    client = TestClient(main.app)
    client.sent = sent  # type: ignore[attr-defined]
    yield client
    main.app.dependency_overrides.clear()
    await app_engine.dispose()


def _set_dummy_auth(monkeypatch, enabled: bool):
    import cocco.main as main

    monkeypatch.setattr(main, "DUMMY_AUTH", enabled)


async def test_the_login_page_is_public(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


async def test_an_anonymous_page_request_is_redirected(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
    # ...and the visitor is sent back where they were headed
    response = client.get("/courses/1/2", follow_redirects=False)
    assert "next=%2Fcourses%2F1%2F2" in response.headers["location"]


async def test_an_anonymous_htmx_request_gets_hx_redirect(client):
    """htmx follows a 302 inside the XHR and would paint the login page into a
    polled fragment; the header makes the browser navigate instead."""
    response = client.get(
        "/f/leaderboard/1",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 204
    assert "/login" in response.headers["HX-Redirect"]
    assert response.text == ""


async def test_dummy_auth_signs_in_without_mail(client, monkeypatch, app_engine):
    _set_dummy_auth(monkeypatch, True)
    response = client.post(
        "/login", data={"email": " Bob@UNIPD.it ", "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.sent == []  # type: ignore[attr-defined]

    async with app_engine.connect() as conn:
        row = await query_user_by_email(conn, "bob@unipd.it")
    assert row is not None and row.nickname == "bob"

    # Signing in again reuses the row rather than creating a second one.
    client.post("/login", data={"email": "bob@unipd.it"}, follow_redirects=False)
    async with app_engine.connect() as conn:
        again = await query_user_by_email(conn, "bob@unipd.it")
    assert again is not None and again.id == row.id


async def test_a_bad_address_is_rejected_before_anything_is_stored(
    client, monkeypatch
):
    _set_dummy_auth(monkeypatch, True)
    response = client.post("/login", data={"email": "not an address"})
    assert response.status_code == 400
    assert "does not look like an email address" in response.text


async def test_the_magic_link_signs_in_once(client, monkeypatch, app_engine):
    _set_dummy_auth(monkeypatch, False)
    response = client.post(
        "/login", data={"email": "carol@unipd.it", "next": "/courses/1/2"}
    )
    assert response.status_code == 200
    assert "Check your inbox" in response.text

    # Nothing exists yet: the account is created when the link is followed, so
    # typing addresses into the form cannot fill the user table.
    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "carol@unipd.it") is None

    [(to, link)] = client.sent  # type: ignore[attr-defined]
    assert to == "carol@unipd.it"

    followed = client.get(link, follow_redirects=False)
    assert followed.status_code == 303
    assert followed.headers["location"] == "/courses/1/2"
    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "carol@unipd.it") is not None

    # The same link a second time is refused: it was spent.
    replayed = client.get(link)
    assert replayed.status_code == 400
    assert "no longer valid" in replayed.text


async def test_an_expired_link_is_refused(client, monkeypatch, app_engine):
    _set_dummy_auth(monkeypatch, False)
    from cocco.auth import hash_token

    now = datetime.now()
    async with app_engine.connect() as conn:
        await insert_login_token(
            conn, hash_token("stale"), "dan@unipd.it", now, now - timedelta(minutes=1)
        )
        await conn.commit()

    response = client.get("/auth/verify?token=stale")
    assert response.status_code == 400
    assert "no longer valid" in response.text
    async with app_engine.connect() as conn:
        assert await query_user_by_email(conn, "dan@unipd.it") is None


async def test_an_unknown_token_is_refused(client):
    assert client.get("/auth/verify?token=nonsense").status_code == 400
    assert client.get("/auth/verify").status_code == 400


async def test_logout_clears_the_session(client, monkeypatch):
    """The pages behind the wall run Postgres-only queries, so what is asserted
    here is the cookie and the redirect - which is what logging out *is*."""
    import cocco.main as main

    _set_dummy_auth(monkeypatch, True)
    client.post("/login", data={"email": "erin@unipd.it"}, follow_redirects=False)
    assert client.cookies.get(main.CONFIG.session.cookie_name)

    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


async def test_a_session_pointing_at_nobody_is_discarded(
    client, monkeypatch, app_engine
):
    """The cookie holds only a user id, so a COCCO_RESET_DB under a live tab
    logs that tab out rather than blowing up."""
    from sqlalchemy import delete

    from cocco.model import User

    _set_dummy_auth(monkeypatch, True)
    client.post("/login", data={"email": "frank@unipd.it"}, follow_redirects=False)

    async with app_engine.connect() as conn:
        await conn.execute(delete(User).where(User.c.email == "frank@unipd.it"))
        await conn.commit()

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert "/login" in response.headers["location"]
