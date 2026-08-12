"""End-to-end tests of the profile page.

The seam between the two halves is `test_admin_routes.py`'s. Refusals happen in
a dependency, and so does every write here - `update_user` and `query_user` are
plain Core, so the whole of "who may change what" fits on SQLite. Only the page
itself needs Postgres: `query_user_profile` is `json_agg`, and those tests are
marked `integration` and drop every table in `$PGDATABASE`.

The rule the second half is really about is the asymmetry between the two
dependencies: a site administrator *reads* somebody's profile and cannot touch
it. Oversight is not impersonation.
"""


import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.avatar import MAX_AVATAR_BYTES, generate_identicon, sniff_image_mime
from cocco.model import (
    Administrator,
    Course,
    Enrollment,
    User,
    UserRole,
    metadata,
)

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
ADMIN = "matteo.ceccarello@unipd.it"

PNG = b"\x89PNG\r\n\x1a\n" + b"the rest is not parsed"
JPEG = b"\xff\xd8\xff\xe0" + b"jfif-ish"


def _write_urls(user_id: int):
    """Every way to change a profile that takes a plain form."""
    return [
        (f"/users/{user_id}/profile", {"nickname": "impostor"}),
        (f"/users/{user_id}/avatar/reset", {}),
    ]


########################################################################
# who may change what


@pytest.fixture
async def app_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(app_engine):
    """Alice teaches a course; bob studies it; matteo administers the site."""
    async with app_engine.connect() as conn:
        ids = {}
        for email, nickname in ((TEACHER, "alice"), (STUDENT, "bob"), (ADMIN, "matteo")):
            image, image_mime = generate_identicon(nickname)
            ids[nickname] = (
                await conn.execute(
                    insert(User)
                    .values(
                        email=email,
                        nickname=nickname,
                        image=image,
                        image_mime=image_mime,
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
        await conn.execute(
            insert(Enrollment).values(
                user_id=ids["bob"], course_id=ids["course"], role=UserRole.student
            )
        )
        await conn.execute(insert(Administrator).values(email=ADMIN))
        await conn.commit()
    return ids


@pytest.fixture
def client(app_engine, monkeypatch):
    import cocco.main as main

    async def _get_db():
        async with app_engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db
    monkeypatch.setattr(main, "DUMMY_AUTH", True)
    monkeypatch.setattr(main, "DUMMY_USER", TEACHER)

    # Not `with TestClient(...)`: entering the context runs the startup event,
    # which reaches for the real Postgres engine.
    client = TestClient(main.app)
    yield client
    main.app.dependency_overrides.clear()


def _sign_in_as(monkeypatch, email: str):
    import cocco.main as main

    monkeypatch.setattr(main, "DUMMY_USER", email)


async def _read_user(app_engine, user_id: int):
    async with app_engine.connect() as conn:
        return (
            await conn.execute(select(User).where(User.c.id == user_id))
        ).one()


async def test_a_stranger_may_not_read_or_change_a_profile(client, world, monkeypatch):
    """Being on the same course as somebody is not oversight of them."""
    _sign_in_as(monkeypatch, STUDENT)
    assert client.get(f"/users/{world['alice']}").status_code == 403
    for url, data in _write_urls(world["alice"]):
        assert client.post(url, data=data).status_code == 403, url
    assert (
        client.post(
            f"/users/{world['alice']}/avatar", files={"avatar_file": ("a.png", PNG)}
        ).status_code
        == 403
    )


async def test_an_administrator_may_look_but_not_touch(
    client, world, app_engine, monkeypatch
):
    """The asymmetry the two dependencies exist for.

    Only the writes are asserted here: the read is `query_user_profile`, which
    is Postgres-only and so is tested in the other half.
    """
    _sign_in_as(monkeypatch, ADMIN)
    for url, data in _write_urls(world["alice"]):
        assert client.post(url, data=data).status_code == 403, url
    assert (
        client.post(
            f"/users/{world['alice']}/avatar", files={"avatar_file": ("a.png", PNG)}
        ).status_code
        == 403
    )
    # And nothing moved.
    assert (await _read_user(app_engine, world["alice"])).nickname == "alice"


async def test_the_name_is_saved(client, world, app_engine):
    response = client.post(
        f"/users/{world['alice']}/profile", data={"nickname": "  Prof. Alice  "}
    )
    assert response.status_code == 200
    assert "Saved." in response.text
    # Trimmed, like every other name this app takes from a form.
    assert (await _read_user(app_engine, world["alice"])).nickname == "Prof. Alice"
    # The navbar shows the same name, so a fresh copy of it comes back too.
    assert "navbar-identity" in response.text
    assert 'hx-swap-oob="true"' in response.text


async def test_an_empty_name_is_refused(client, world, app_engine):
    """200 with a sentence, not a 422: htmx drops the body of an error."""
    response = client.post(f"/users/{world['alice']}/profile", data={"nickname": "   "})
    assert response.status_code == 200
    assert "A profile needs a name." in response.text
    assert (await _read_user(app_engine, world["alice"])).nickname == "alice"
    # No alert, no out-of-band swap: the navbar still says the truth.
    assert 'hx-swap-oob="true"' not in response.text


async def test_a_missing_field_is_not_a_422(client, world):
    """The box can be cleared away entirely, and the answer is still a page."""
    assert client.post(f"/users/{world['alice']}/profile", data={}).status_code == 200


async def test_an_overlong_name_is_refused(client, world, app_engine):
    import cocco.main as main

    response = client.post(
        f"/users/{world['alice']}/profile",
        data={"nickname": "x" * (main.MAX_NICKNAME_CHARS + 1)},
    )
    assert response.status_code == 200
    assert "longer than" in response.text
    assert (await _read_user(app_engine, world["alice"])).nickname == "alice"


async def test_the_address_is_never_changed(client, world, app_engine):
    """`email` is the login identifier Google proves, so the form has no field
    for it and sending one anyway changes nothing."""
    client.post(
        f"/users/{world['alice']}/profile",
        data={"nickname": "alice", "email": "root@unipd.it"},
    )
    assert (await _read_user(app_engine, world["alice"])).email == TEACHER


async def test_an_uploaded_picture_becomes_the_avatar(client, world, app_engine):
    response = client.post(
        f"/users/{world['alice']}/avatar", files={"avatar_file": ("me.png", PNG)}
    )
    assert response.status_code == 200
    row = await _read_user(app_engine, world["alice"])
    assert row.image == PNG
    assert row.image_mime == "image/png"


async def test_the_stored_mime_is_sniffed_not_believed(client, world, app_engine):
    """The upload's own `content_type` is written by whoever is uploading, and
    `user_avatar` serves the bytes back under the stored mime from this
    origin."""
    client.post(
        f"/users/{world['alice']}/avatar",
        files={"avatar_file": ("me.png", JPEG, "image/png")},
    )
    assert (await _read_user(app_engine, world["alice"])).image_mime == "image/jpeg"


async def test_an_svg_is_refused(client, world, app_engine):
    """The rule `sniff_image_mime` exists for: these bytes come back from this
    origin, so an uploaded SVG would be a script running as the site."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    response = client.post(
        f"/users/{world['alice']}/avatar", files={"avatar_file": ("me.svg", svg)}
    )
    assert response.status_code == 200
    assert "not a PNG, JPEG, GIF or WebP" in response.text
    assert (await _read_user(app_engine, world["alice"])).image != svg


async def test_an_oversized_picture_is_refused(client, world, app_engine):
    huge = PNG + b"\x00" * MAX_AVATAR_BYTES
    response = client.post(
        f"/users/{world['alice']}/avatar", files={"avatar_file": ("me.png", huge)}
    )
    assert response.status_code == 200
    assert "the limit is" in response.text
    assert (await _read_user(app_engine, world["alice"])).image != huge


async def test_resetting_regenerates_from_the_current_name(client, world, app_engine):
    """The identicon is seeded on the nickname at sign-up and never follows a
    rename on its own — this button is how it catches up."""
    client.post(f"/users/{world['alice']}/avatar", files={"avatar_file": ("a.png", PNG)})
    client.post(f"/users/{world['alice']}/profile", data={"nickname": "Prof. Alice"})

    response = client.post(f"/users/{world['alice']}/avatar/reset")
    assert response.status_code == 200
    expected, expected_mime = generate_identicon("Prof. Alice")
    row = await _read_user(app_engine, world["alice"])
    assert (row.image, row.image_mime) == (expected, expected_mime)


async def test_the_avatar_is_served_without_sniffing(client, world):
    """Second lock on the same door as `sniff_image_mime`: the bytes are user
    input served from this origin."""
    response = client.get(f"/users/{world['alice']}/avatar")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]
    # The URL does not change when the picture does.
    assert response.headers["cache-control"] == "no-cache"


def test_only_the_four_raster_formats_are_pictures():
    assert sniff_image_mime(PNG) == "image/png"
    assert sniff_image_mime(JPEG) == "image/jpeg"
    assert sniff_image_mime(b"GIF89a...") == "image/gif"
    assert sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_mime(b"<svg></svg>") is None
    assert sniff_image_mime(b"") is None
    # A PNG's magic has to be at the front, not somewhere inside.
    assert sniff_image_mime(b"GIF" + PNG) is None


########################################################################
# the page itself
#
# Postgres, because `query_user_profile` is `json_agg`.


@pytest.fixture
async def pg_client(monkeypatch, postgres_url):
    """The profile page over a live Postgres, signed in as Alice.

    Drops every table in `$PGDATABASE` first, as the other integration tests do.
    """
    import cocco.main as main

    sync_engine = create_engine(postgres_url)
    metadata.drop_all(sync_engine)
    metadata.create_all(sync_engine)
    ids = {}
    with sync_engine.begin() as conn:
        for email, nickname in ((TEACHER, "alice"), (STUDENT, "bob"), (ADMIN, "matteo")):
            image, image_mime = generate_identicon(nickname)
            ids[nickname] = conn.execute(
                insert(User)
                .values(
                    email=email,
                    nickname=nickname,
                    image=image,
                    image_mime=image_mime,
                )
                .returning(User.c.id)
            ).scalar_one()
        ids["course"] = conn.execute(
            insert(Course)
            .values(title="Algorithms 101", description="...")
            .returning(Course.c.id)
        ).scalar_one()
        conn.execute(
            insert(Enrollment).values(
                user_id=ids["bob"], course_id=ids["course"], role=UserRole.student
            )
        )
        conn.execute(insert(Administrator).values(email=ADMIN))

    engine = create_async_engine(postgres_url)

    async def _get_db():
        async with engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db
    monkeypatch.setattr(main, "DUMMY_AUTH", True)
    monkeypatch.setattr(main, "DUMMY_USER", TEACHER)

    client = TestClient(main.app)
    yield client, ids
    main.app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.integration
async def test_your_own_profile_is_a_form(pg_client):
    client, ids = pg_client
    response = client.get(f"/users/{ids['alice']}")
    assert response.status_code == 200
    assert 'name="nickname"' in response.text
    assert f"/users/{ids['alice']}/avatar/reset" in response.text
    # Everything but the address, which has no input anywhere on the page.
    assert TEACHER in response.text
    assert 'name="email"' not in response.text


@pytest.mark.integration
async def test_an_administrator_sees_the_same_facts_as_text(pg_client, monkeypatch):
    client, ids = pg_client
    _sign_in_as(monkeypatch, ADMIN)
    response = client.get(f"/users/{ids['bob']}")
    assert response.status_code == 200
    # What we know about them: the address, the name, the class list.
    assert STUDENT in response.text
    assert "bob" in response.text
    assert "Algorithms 101" in response.text
    # And no way to change any of it.
    assert 'name="nickname"' not in response.text
    assert f"/users/{ids['bob']}/avatar/reset" not in response.text


@pytest.mark.integration
async def test_a_profile_that_is_not_there_is_a_404(pg_client, monkeypatch):
    client, _ = pg_client
    _sign_in_as(monkeypatch, ADMIN)
    assert client.get("/users/99999").status_code == 404


@pytest.mark.integration
async def test_a_stranger_gets_a_page_and_not_a_traceback(pg_client, monkeypatch):
    client, ids = pg_client
    _sign_in_as(monkeypatch, STUDENT)
    response = client.get(f"/users/{ids['alice']}")
    assert response.status_code == 403
    assert "You can only look at your own profile." in response.text


@pytest.mark.integration
async def test_the_navbar_offers_your_own_profile(pg_client):
    """The second way in, beside the admin list."""
    client, ids = pg_client
    response = client.get("/")
    assert response.status_code == 200
    assert f'href="http://testserver/users/{ids["alice"]}"' in response.text
