"""End-to-end tests of the admin page.

One page, one swap target and one fixture: every route here - the filter box's
GET and all three mutations - answers with `f-users.jinja`, alert included, so
there is nothing on this page that does not go through `query_users`. Which is
`json_agg`, like every other page query in this app, and that is the ordinary
case rather than a special one worth splitting a file over.
"""


import pytest
from sqlalchemy import insert, select

from cocco.conftest import sign_in_as
from cocco.model import (
    Administrator,
    Course,
    Enrollment,
    User,
    UserRole,
)

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
ADMIN = "matteo.ceccarello@unipd.it"

# Every way into the admin page. Teaching a course is not administering the
# site, so none of these is open to Alice - and hiding the button on the home
# page is not what stops her.
def _admin_urls(user_id: int):
    return [
        ("get", "/admin", None),
        ("get", "/f/users", None),
        ("post", "/admin/administrators", {"email": "eve@unipd.it"}),
        ("post", f"/admin/users/{user_id}/block", {"reason": "no"}),
        ("post", f"/admin/users/{user_id}/unblock", {}),
    ]


@pytest.fixture
async def world(app_engine):
    """Alice teaches a course; bob studies it; matteo administers the site.

    Alice is the interesting one: she is `teacher` on a course, which is full
    staff *there* and nothing at all here.
    """
    async with app_engine.connect() as conn:
        ids = {}
        for email, nickname in ((TEACHER, "alice"), (STUDENT, "bob"), (ADMIN, "matteo")):
            ids[nickname] = (
                await conn.execute(
                    insert(User)
                    .values(
                        email=email,
                        nickname=nickname,
                        image=b"",
                        image_mime="image/svg+xml",
                    )
                    .returning(User.c.id)
                )
            ).scalar_one()
        ids["course"] = (
            await conn.execute(
                insert(Course)
                .values(title="Algorithms 101", description="...")
                .returning(Course.c.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(Enrollment).values(
                user_id=ids["alice"], course_id=ids["course"], role=UserRole.teacher
            )
        )
        await conn.execute(insert(Administrator).values(email=ADMIN))
        await conn.commit()
    return ids


@pytest.fixture
def client(make_client, world):
    # `world` is a dependency rather than an argument of the tests alone,
    # because signing in *creates* the account if it is missing: without the
    # ordering, the fixture would insert alice a moment before the seed does and
    # the seed would trip over its own unique index.
    return make_client(as_=ADMIN)


def _emails_in(engine, table):
    with engine.connect() as conn:
        return sorted((conn.execute(select(table.c.email))).scalars().all())


async def test_the_admin_page_is_administrators_only(client, world, monkeypatch):
    for email in (TEACHER, STUDENT):
        sign_in_as(client, email)
        for method, url, data in _admin_urls(world["bob"]):
            response = (
                client.get(url) if method == "get" else client.post(url, data=data)
            )
            assert response.status_code == 403, f"{method} {url}"


async def test_the_refusal_names_the_permission_not_a_course(
    client, world, monkeypatch
):
    """`Admin` guards two things now. A sentence about creating a course would
    be the wrong one on half of them, and wronger still on a third."""
    sign_in_as(client, TEACHER)
    response = client.get("/admin")
    assert response.status_code == 403
    assert "Only a site administrator can do that." in response.text
    assert "can create a course" not in response.text


async def test_the_page_lists_everyone(client, db):
    response = client.get("/admin")
    assert response.status_code == 200
    for email in (TEACHER, STUDENT, ADMIN):
        assert email in response.text


async def test_appointing_an_administrator(client, db):
    # Normalized on the way in: this is the only place in the app where a human
    # types an address.
    response = client.post(
        "/admin/administrators", data={"email": "  Eve@UNIPD.it "}
    )
    assert response.status_code == 200
    assert "eve@unipd.it is now an administrator" in response.text
    assert _emails_in(db, Administrator) == ["eve@unipd.it", ADMIN]

    # Twice is a no-op, not a primary key violation - ON CONFLICT DO NOTHING.
    assert client.post(
        "/admin/administrators", data={"email": "eve@unipd.it"}
    ).status_code == 200
    assert _emails_in(db, Administrator) == ["eve@unipd.it", ADMIN]

    # Appointed and never signed in, which is what the table is keyed on an
    # address for - and what the second list on the page is for.
    assert "Appointed, never signed in" in client.get("/admin").text


async def test_appointing_refuses_an_address_nobody_could_sign_in_with(client, db):
    """`_load_auth_config` only warns about this, because a bad line in
    `cocco.toml` must not stop the app booting. A form has somebody in front of
    it who can fix the typo."""
    for bad in ("not-an-address", "eve@gmail.com", ""):
        response = client.post("/admin/administrators", data={"email": bad})
        # 200 with an alert, never a 4xx: htmx does not swap an error response.
        assert response.status_code == 200
        assert _emails_in(db, Administrator) == [ADMIN]


async def test_blocking_and_unblocking_a_user(client, db):
    with db.connect() as conn:
        bob = conn.execute(select(User.c.id).where(User.c.email == STUDENT)).scalar_one()

    response = client.post(
        f"/admin/users/{bob}/block", data={"reason": "spamming the judge"}
    )
    assert response.status_code == 200
    assert "now blocked" in response.text
    assert "spamming the judge" in response.text

    response = client.post(f"/admin/users/{bob}/unblock", data={})
    assert response.status_code == 200
    with db.connect() as conn:
        row = conn.execute(select(User.c.blocked_at).where(User.c.id == bob)).first()
    assert row.blocked_at is None


async def test_an_administrator_cannot_be_blocked_through_the_page(client, db):
    """The lockout guard, from the outside: appointing is one-way through the
    app, so a blocked administrator could never be let back in."""
    with db.connect() as conn:
        matteo = conn.execute(select(User.c.id).where(User.c.email == ADMIN)).scalar_one()

    response = client.post(f"/admin/users/{matteo}/block", data={"reason": "oops"})
    assert response.status_code == 200
    assert "an administrator cannot be" in response.text.lower()
    with db.connect() as conn:
        row = conn.execute(select(User.c.blocked_at).where(User.c.id == matteo)).first()
    assert row.blocked_at is None


async def test_a_blocked_address_cannot_be_appointed(client, db):
    """The other half of the same guard: block Bob today, appoint him tomorrow,
    and the site would have an administrator who cannot sign in."""
    with db.connect() as conn:
        bob = conn.execute(select(User.c.id).where(User.c.email == STUDENT)).scalar_one()
    client.post(f"/admin/users/{bob}/block", data={"reason": "spamming"})

    response = client.post("/admin/administrators", data={"email": STUDENT})
    assert response.status_code == 200
    assert "Unblock them first" in response.text
    assert _emails_in(db, Administrator) == [ADMIN]


async def test_a_mutation_keeps_the_filter(client, db):
    """Blocking somebody under a filter leaves the filter in force - the same
    reason `post_course_enroll` carries `q`."""
    with db.connect() as conn:
        bob = conn.execute(select(User.c.id).where(User.c.email == STUDENT)).scalar_one()

    response = client.post(
        f"/admin/users/{bob}/block", data={"reason": "spamming", "q": "bob"}
    )
    assert response.status_code == 200
    assert STUDENT in response.text
    # Alice does not match "bob", so the answer is still the filtered list.
    assert TEACHER not in response.text
