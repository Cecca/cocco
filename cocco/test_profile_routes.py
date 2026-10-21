"""End-to-end tests of the profile page.

One URL and two pages - the owner gets forms, a site administrator gets the same
facts as text - so the rule this file is really about is the asymmetry between
the two dependencies. `require_profile_reader` allows either and reports which;
`require_profile_owner` has no administrator branch at all. Oversight is not
impersonation, and a site administrator may block an account without ever being
able to rename it.
"""


import pytest
from sqlalchemy import insert, select

from cocco.avatar import MAX_AVATAR_BYTES, generate_identicon, sniff_image_mime
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

PNG = b"\x89PNG\r\n\x1a\n" + b"the rest is not parsed"
JPEG = b"\xff\xd8\xff\xe0" + b"jfif-ish"

# Long enough for `check_password`, so a refusal in these tests is about who is
# asking rather than about the password itself.
PASSWORD = "a perfectly fine password"
OTHER_PASSWORD = "another perfectly fine password"


def _write_urls(user_id: int):
    """Every way to change a profile that takes a plain form.

    The two password routes belong here even though `[auth] password_login` is
    off in these tests: `ProfileOwner` is a dependency and runs before the body
    that would 404, so a stranger is turned away by the same 403 as everywhere
    else on this page - which is the property being asserted.
    """
    return [
        (f"/users/{user_id}/profile", {"nickname": "impostor"}),
        (f"/users/{user_id}/avatar/reset", {}),
        (f"/users/{user_id}/password", {"password": PASSWORD, "confirm": PASSWORD}),
        (f"/users/{user_id}/password/delete", {}),
    ]


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
def client(make_client, world):
    # `world` is a dependency rather than an argument of the tests alone,
    # because signing in *creates* the account if it is missing: without the
    # ordering, the fixture would insert alice a moment before the seed does and
    # the seed would trip over its own unique index.
    return make_client(as_=TEACHER)


async def _read_user(app_engine, user_id: int):
    async with app_engine.connect() as conn:
        return (
            await conn.execute(select(User).where(User.c.id == user_id))
        ).one()


async def test_a_stranger_may_not_read_or_change_a_profile(client, world, monkeypatch):
    """Being on the same course as somebody is not oversight of them."""
    sign_in_as(client, STUDENT)
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
    sign_in_as(client, ADMIN)
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
    from cocco.web import profile

    response = client.post(
        f"/users/{world['alice']}/profile",
        data={"nickname": "x" * (profile.MAX_NICKNAME_CHARS + 1)},
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


async def test_your_own_profile_is_a_form(client, world):
    response = client.get(f"/users/{world['alice']}")
    assert response.status_code == 200
    assert 'name="nickname"' in response.text
    assert f"/users/{world['alice']}/avatar/reset" in response.text
    # Everything but the address, which has no input anywhere on the page.
    assert TEACHER in response.text
    assert 'name="email"' not in response.text


async def test_an_administrator_sees_the_same_facts_as_text(client, world, monkeypatch):
    sign_in_as(client, ADMIN)
    response = client.get(f"/users/{world['bob']}")
    assert response.status_code == 200
    # What we know about them: the address, the name, the class list.
    assert STUDENT in response.text
    assert "bob" in response.text
    assert "Algorithms 101" in response.text
    # And no way to change any of it.
    assert 'name="nickname"' not in response.text
    assert f"/users/{world['bob']}/avatar/reset" not in response.text


async def test_a_profile_that_is_not_there_is_a_404(client, world, monkeypatch):
    sign_in_as(client, ADMIN)
    assert client.get("/users/99999").status_code == 404


async def test_a_stranger_gets_a_page_and_not_a_traceback(client, world, monkeypatch):
    sign_in_as(client, STUDENT)
    response = client.get(f"/users/{world['alice']}")
    assert response.status_code == 403
    assert "You can only look at your own profile." in response.text


async def test_the_navbar_offers_your_own_profile(client, world):
    """The second way in, beside the admin list."""
    response = client.get("/")
    assert response.status_code == 200
    assert f'href="http://testserver/users/{world["alice"]}"' in response.text


########################################################################
# The password section
#
# Behind `ProfileOwner`, like the tokens beside it and for the tokens' reason
# one step further along: reading a profile is oversight, and setting somebody
# else's password is the ability to become them. Behind `[auth] password_login`
# as well, and that half is a 404 rather than a hidden form - the rule
# `post_auth_dummy` states.


@pytest.fixture
def passwords(monkeypatch):
    """`[auth] password_login` on, as `test_password_routes.py` turns it on."""
    from dataclasses import replace

    from cocco.web import core

    monkeypatch.setattr(
        core,
        "CONFIG",
        replace(core.CONFIG, auth=replace(core.CONFIG.auth, password_login=True)),
    )


def _password_state(client, user_id: int):
    return client.get(f"/f/users/{user_id}/password")


async def test_the_section_is_absent_without_the_setting(client, world):
    """Not merely undrawn: the routes are gone."""
    body = client.get(f"/users/{world['alice']}").text
    assert "password" not in body.lower() or "/password" not in body
    assert _password_state(client, world["alice"]).status_code == 404
    assert (
        client.post(
            f"/users/{world['alice']}/password",
            data={"password": PASSWORD, "confirm": PASSWORD},
        ).status_code
        == 404
    )


async def test_the_owner_sets_a_first_password_without_the_old_one(
    client, world, passwords
):
    """There is nothing to ask for before the first password - the session is
    the only thing that could have reached the form."""
    response = client.post(
        f"/users/{world['alice']}/password",
        data={"password": PASSWORD, "confirm": PASSWORD},
    )
    assert response.status_code == 200
    assert "Password set." in response.text
    assert "A password is set" in _password_state(client, world["alice"]).text


async def test_changing_one_needs_the_current_one(client, world, passwords):
    """A session left open on a shared machine is the ordinary way an account
    is taken; without this the person who found it could lock its owner out."""
    client.post(
        f"/users/{world['alice']}/password",
        data={"password": PASSWORD, "confirm": PASSWORD},
    )

    refused = client.post(
        f"/users/{world['alice']}/password",
        data={
            "current_password": "not it",
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    assert "not your current password" in refused.text

    accepted = client.post(
        f"/users/{world['alice']}/password",
        data={
            "current_password": PASSWORD,
            "password": OTHER_PASSWORD,
            "confirm": OTHER_PASSWORD,
        },
    )
    assert "Password changed." in accepted.text


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"password": "short", "confirm": "short"}, "12"),
        ({"password": PASSWORD, "confirm": OTHER_PASSWORD}, "not the same"),
    ],
)
async def test_a_password_that_will_not_do_says_why(
    client, world, passwords, data, fragment
):
    response = client.post(f"/users/{world['alice']}/password", data=data)
    assert fragment in response.text
    assert "No password is set" in _password_state(client, world["alice"]).text


async def test_the_owner_can_take_it_away_again(client, world, passwords):
    client.post(
        f"/users/{world['alice']}/password",
        data={"password": PASSWORD, "confirm": PASSWORD},
    )
    response = client.post(f"/users/{world['alice']}/password/delete")
    assert "Password removed." in response.text
    assert "No password is set" in _password_state(client, world["alice"]).text


async def test_an_administrator_cannot_read_or_set_one(client, world, passwords):
    """`ProfileOwner`, never `ProfileReader`. An administrator who has to get an
    account back in issues a password from `/admin` instead - which is shown to
    them once and expected to be replaced from here."""
    sign_in_as(client, ADMIN)
    assert _password_state(client, world["alice"]).status_code == 403
    assert (
        client.post(
            f"/users/{world['alice']}/password",
            data={"password": PASSWORD, "confirm": PASSWORD},
        ).status_code
        == 403
    )
