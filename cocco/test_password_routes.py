"""End-to-end tests of password sign-in, behind `[auth] password_login`.

Nothing is faked here, unlike `test_auth_routes.py`: there is no network in
this way in, so the real routes, the real scrypt and the real statements run.
The parameters make each hash cost real work, which is why this file signs in
as few times as it can get away with.

Two properties are worth naming, because they are the ones a refactor would
quietly lose:

  * **Registration is a claim, and only of an unused account.**
    `claim_user_password` writes only where `first_login_at IS NULL`, so a
    roster a teacher wrote out before term can be claimed by its owner and an
    account somebody has actually signed in with cannot be taken over.

  * **Every way a sign-in can be wrong is one sentence.** An unknown address,
    an account with no password and a wrong password are indistinguishable
    from outside; anything else is a way to read a class list off the login
    page.
"""

from dataclasses import replace

import pytest
from sqlalchemy import insert, select

from cocco.model import User, UserPassword
from cocco.passwords import MIN_PASSWORD_LENGTH, hash_password
from cocco.repository.users import block_user, record_sign_in

GOOD = "a perfectly fine password"
OTHER = "another perfectly fine password"
DAVE = "dave@unipd.it"

# `RESTART IDENTITY` between tests means the first account created is row 1,
# and an avatar is the cheapest thing behind `CurrentUser` there is.
BEHIND_THE_WALL = "/users/1/avatar"


def _signed_in(client) -> bool:
    """Whether the client holds a session. Without `follow_redirects=False`
    this would follow the 303 to `/login` and read its 200 as a yes."""
    return client.get(BEHIND_THE_WALL, follow_redirects=False).status_code == 200


@pytest.fixture
def passwords(monkeypatch):
    """Turn `[auth] password_login` on, the way `google` turns Google on:
    `dataclasses.replace` on the frozen config, no filesystem involved."""
    import cocco.main as main

    monkeypatch.setattr(
        main,
        "CONFIG",
        replace(
            main.CONFIG,
            auth=replace(main.CONFIG.auth, password_login=True),
        ),
    )


@pytest.fixture
def client(make_client):
    """The development shortcut off: it bypasses everything this file is
    about."""
    return make_client(dummy_auth=False)


async def _account(
    app_engine, email: str, *, password: str | None = None, signed_in=False
):
    """An account as some other part of the app would have left it - a teacher's
    roster entry, a Google user, somebody who already registered."""
    async with app_engine.connect() as conn:
        user_id = (
            await conn.execute(
                insert(User)
                .values(
                    email=email,
                    nickname=email.split("@")[0],
                    image=b"",
                    image_mime="image/svg+xml",
                )
                .returning(User.c.id)
            )
        ).scalar_one()
        if password is not None:
            await conn.execute(
                insert(UserPassword).values(
                    user_id=user_id, password_hash=hash_password(password)
                )
            )
        if signed_in:
            await record_sign_in(conn, user_id)
        await conn.commit()
    return user_id


async def _block(app_engine, user_id: int, reason: str) -> None:
    async with app_engine.connect() as conn:
        await block_user(conn, user_id, reason)
        await conn.commit()


async def _rows(app_engine, stmt):
    async with app_engine.connect() as conn:
        return (await conn.execute(stmt)).all()


########################################################################
# the switch
#
# With it off the feature does not exist: not a hidden form, but a 404. That is
# `post_auth_dummy`'s rule - a deployment should not admit that a path it does
# not serve is there - and hiding the form is presentation, never the check.


def test_the_login_page_offers_no_password_form_by_default(client):
    body = client.get("/login").text
    assert "/auth/password" not in body


def test_the_login_page_offers_one_when_it_is_configured(client, passwords):
    body = client.get("/login").text
    assert "/auth/password" in body
    assert "I do not have an account yet" in body


@pytest.mark.parametrize(
    "path", ["/auth/password", "/auth/password/register"]
)
def test_the_routes_are_absent_rather_than_forbidden(client, path):
    response = client.post(path, data={"email": DAVE, "password": GOOD})
    assert response.status_code == 404


########################################################################
# registering


async def test_registering_creates_the_account_and_signs_it_in(
    client, passwords, app_engine
):
    response = client.post(
        "/auth/password/register",
        data={"email": DAVE, "password": GOOD, "confirm": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert _signed_in(client)

    rows = await _rows(app_engine, select(User.c.email))
    assert [r.email for r in rows] == [DAVE]


async def test_a_roster_entry_is_claimed_by_its_owner(client, passwords, app_engine):
    """The case the rule is shaped around: a teacher writes an address onto a
    course before term, so the account exists and has never been signed in."""
    user_id = await _account(app_engine, DAVE)

    response = client.post(
        "/auth/password/register",
        data={"email": DAVE, "password": GOOD, "confirm": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 303

    # The same account, not a second one.
    rows = await _rows(app_engine, select(User.c.id))
    assert [r.id for r in rows] == [user_id]


async def test_an_account_that_has_signed_in_cannot_be_claimed(
    client, passwords, app_engine
):
    """The whole of what stops password login being an account takeover."""
    await _account(app_engine, DAVE, signed_in=True)

    response = client.post(
        "/auth/password/register",
        data={"email": DAVE, "password": GOOD, "confirm": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert "already has an account" in response.text
    assert await _rows(app_engine, select(UserPassword)) == []


async def test_an_address_is_claimed_only_once(client, passwords, app_engine):
    await _account(app_engine, DAVE, password=OTHER)

    response = client.post(
        "/auth/password/register",
        data={"email": DAVE, "password": GOOD, "confirm": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 409

    # And the password that was there is untouched.
    from cocco.passwords import verify_password

    rows = await _rows(app_engine, select(UserPassword.c.password_hash))
    assert verify_password(OTHER, rows[0].password_hash)


async def test_an_address_outside_the_allowlist_never_becomes_a_row(
    client, passwords, app_engine
):
    """The allowlist is checked inside `_complete_sign_in`, before
    `get_or_create_user`, which is what makes this true rather than merely
    refused."""
    response = client.post(
        "/auth/password/register",
        data={
            "email": "dave@gmail.com",
            "password": GOOD,
            "confirm": GOOD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "institutional address" in response.text
    assert await _rows(app_engine, select(User)) == []


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"email": "", "password": GOOD, "confirm": GOOD}, "email address"),
        ({"email": DAVE, "password": "short", "confirm": "short"},
         str(MIN_PASSWORD_LENGTH)),
        ({"email": DAVE, "password": GOOD, "confirm": OTHER}, "not the same"),
    ],
)
async def test_a_registration_form_says_what_is_wrong_with_it(
    client, passwords, app_engine, data, fragment
):
    """Specific sentences here, unlike signing in: this is a form somebody is
    filling in, not a credential somebody is presenting, so it tells them
    nothing they could not learn by trying it on their own address."""
    response = client.post(
        "/auth/password/register", data=data, follow_redirects=False
    )
    assert response.status_code == 400
    assert fragment in response.text
    assert await _rows(app_engine, select(User)) == []


async def test_a_blocked_account_is_refused_rather_than_claimed(
    client, passwords, app_engine
):
    await _account(app_engine, DAVE)
    await _block(app_engine, 1, "cheating")

    response = client.post(
        "/auth/password/register",
        data={"email": DAVE, "password": GOOD, "confirm": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "blocked" in response.text
    assert not _signed_in(client)


########################################################################
# signing in


async def test_the_right_password_signs_in(client, passwords, app_engine):
    await _account(app_engine, DAVE, password=GOOD)

    response = client.post(
        "/auth/password",
        data={"email": DAVE, "password": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _signed_in(client)


async def test_signing_in_records_the_first_login(client, passwords, app_engine):
    """Which is also what makes the account unclaimable from then on - the two
    are the same column, deliberately."""
    await _account(app_engine, DAVE, password=GOOD)
    client.post(
        "/auth/password",
        data={"email": DAVE, "password": GOOD},
        follow_redirects=False,
    )
    rows = await _rows(app_engine, select(User.c.first_login_at))
    assert rows[0].first_login_at is not None


@pytest.mark.parametrize(
    "setup, password",
    [
        # A wrong password on an account that has one.
        ("password", OTHER),
        # An account that exists and has never set one.
        ("no password", GOOD),
        # Nobody has that address at all.
        ("no account", GOOD),
        # Not even an address.
        ("no account", ""),
    ],
)
async def test_every_wrong_sign_in_is_the_same_sentence(
    client, passwords, app_engine, setup, password
):
    """The property, pinned. Telling these apart - by sentence, by status, or
    by returning early for an address nobody has - turns the login page into a
    way of reading a class list."""
    if setup == "password":
        await _account(app_engine, DAVE, password=GOOD)
    elif setup == "no password":
        await _account(app_engine, DAVE)

    response = client.post(
        "/auth/password",
        data={"email": DAVE, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "That address and password do not match." in response.text
    assert not _signed_in(client)


async def test_an_unusable_address_is_that_same_sentence_too(
    client, passwords, app_engine
):
    """`normalize_email` raising is not a different event from a wrong
    password: saying so would tell an attacker which of their guesses was even
    a candidate."""
    response = client.post(
        "/auth/password",
        data={"email": "not-an-email", "password": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "That address and password do not match." in response.text


async def test_a_blocked_account_with_the_right_password_is_still_refused(
    client, passwords, app_engine
):
    await _account(app_engine, DAVE, password=GOOD)
    await _block(app_engine, 1, "cheating")

    response = client.post(
        "/auth/password",
        data={"email": DAVE, "password": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "blocked" in response.text


async def test_the_address_is_matched_after_normalizing(client, passwords, app_engine):
    await _account(app_engine, DAVE, password=GOOD)
    response = client.post(
        "/auth/password",
        data={"email": "  DAVE@UNIPD.IT ", "password": GOOD},
        follow_redirects=False,
    )
    assert response.status_code == 303


########################################################################
# where a sign-in lands


async def test_next_is_honoured(client, passwords, app_engine):
    await _account(app_engine, DAVE, password=GOOD)
    response = client.post(
        "/auth/password",
        data={"email": DAVE, "password": GOOD, "next": "/courses/7"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/courses/7"


@pytest.mark.parametrize(
    "hostile", ["https://evil.example/", "//evil.example/", "http://evil"]
)
async def test_a_hostile_next_is_filtered(client, passwords, app_engine, hostile):
    await _account(app_engine, DAVE, password=GOOD)
    response = client.post(
        "/auth/password",
        data={"email": DAVE, "password": GOOD, "next": hostile},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


async def test_a_registration_lands_where_it_was_sent(client, passwords, app_engine):
    response = client.post(
        "/auth/password/register",
        data={
            "email": DAVE,
            "password": GOOD,
            "confirm": GOOD,
            "next": "/courses/7",
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/courses/7"
