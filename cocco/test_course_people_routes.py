"""End-to-end tests of a course's people page.

In the two halves `test_admin_routes.py` splits into, and for the same reason: a
refusal happens in a *dependency*, so the route body - and with it
`query_course_people`, which is `json_agg` and therefore Postgres-only - never
runs. So "who is turned away" is a unit test on SQLite.

What the page *does* is the other half, and none of it fits on SQLite: every
route here, including every refusal, answers with the roster. Those are marked
`integration` and talk to the real server, which they **drop every table of** -
the same bargain `test_repository.py` makes.
"""


import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.model import (
    Administrator,
    Challenge,
    Course,
    Enrollment,
    Submission,
    SubmissionStatus,
    User,
    UserRole,
    metadata,
)

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
ADMIN = "matteo.ceccarello@unipd.it"
# Teaches the *other* course, and nothing on this one. The whole point of
# `require_course_editor` being keyed on the course: staff of one course are
# strangers on every other.
OUTSIDER = "dana@unipd.it"


def _people_urls(course_id: int, user_id: int):
    """Every way into the people page, for a course somebody is not staff on."""
    return [
        ("get", f"/courses/{course_id}/people", None),
        ("get", f"/f/courses/{course_id}/people", None),
        ("post", f"/courses/{course_id}/people",
         {"email": "eve@unipd.it", "new_role": "student"}),
        ("post", f"/courses/{course_id}/people/{user_id}/role",
         {"new_role": "teacher"}),
        ("post", f"/courses/{course_id}/people/{user_id}/remove", {}),
    ]


########################################################################
# who is turned away


@pytest.fixture
async def app_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(app_engine):
    """Alice teaches a course, Bob studies it, Dana teaches a different one.

    Matteo administers the site and is enrolled on neither, which is what makes
    him the interesting case: `effective_course_role` has to let him in anyway.
    """
    async with app_engine.connect() as conn:
        ids = {}
        for email, nickname in (
            (TEACHER, "alice"),
            (STUDENT, "bob"),
            (ADMIN, "matteo"),
            (OUTSIDER, "dana"),
        ):
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
        for key, title in (("course", "Algorithms 101"), ("other", "Data Structures")):
            ids[key] = (
                await conn.execute(
                    insert(Course)
                    .values(title=title, description="...")
                    .returning(Course.c.id)
                )
            ).scalar_one()
        for nickname, course, role in (
            ("alice", "course", UserRole.teacher),
            ("bob", "course", UserRole.student),
            ("dana", "other", UserRole.teacher),
        ):
            await conn.execute(
                insert(Enrollment).values(
                    user_id=ids[nickname], course_id=ids[course], role=role
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


async def test_the_people_page_is_course_staff_only(client, world, monkeypatch):
    """A student of the course is not staff on it, and staff of another course
    are not staff here - the case `require_course_editor` being keyed on the
    course in the path exists for."""
    course_id = world["course"]
    for email in (STUDENT, OUTSIDER):
        _sign_in_as(monkeypatch, email)
        for method, url, data in _people_urls(course_id, world["bob"]):
            response = (
                client.get(url) if method == "get" else client.post(url, data=data)
            )
            assert response.status_code == 403, f"{email}: {method} {url}"


async def test_the_refusal_names_the_course_not_the_site(client, world, monkeypatch):
    """`Admin`'s sentence would be the wrong one here: this is a per-course
    permission, and a site administrator passes it without being one."""
    _sign_in_as(monkeypatch, STUDENT)
    response = client.get(f"/courses/{world['course']}/people")
    assert response.status_code == 403
    assert "Only this course's staff can do that." in response.text


# The administrator's side of this - that being staff on every course gets him
# in without an enrollment - is asserted in the Postgres half below. There is
# no route on this page that renders nothing Postgres-only, so a 200 cannot be
# claimed here at all: every one of them answers with the roster.


########################################################################
# what the page does
#
# Postgres, because `query_course_people` is `json_agg` and every route here
# answers with it - including the ones that refuse.


@pytest.fixture
async def pg_client(monkeypatch, postgres_url):
    """The people page over a live Postgres, signed in as the course's teacher.

    Drops every table in `$PGDATABASE` first, as the repository integration
    tests do. Bob has a submission on the course, so the "their work stays"
    assertion has something to be about.
    """
    import cocco.main as main

    sync_engine = create_engine(postgres_url)
    metadata.drop_all(sync_engine)
    metadata.create_all(sync_engine)
    ids = {}
    with sync_engine.begin() as conn:
        for email, nickname in (
            (TEACHER, "alice"),
            (STUDENT, "bob"),
            (ADMIN, "matteo"),
            (OUTSIDER, "dana"),
        ):
            ids[nickname] = conn.execute(
                insert(User)
                .values(
                    email=email,
                    nickname=nickname,
                    image=b"",
                    image_mime="image/svg+xml",
                )
                .returning(User.c.id)
            ).scalar_one()
        ids["course"] = conn.execute(
            insert(Course)
            .values(title="Algorithms 101", description="...")
            .returning(Course.c.id)
        ).scalar_one()
        for nickname, role in (
            ("alice", UserRole.teacher),
            ("bob", UserRole.student),
        ):
            conn.execute(
                insert(Enrollment).values(
                    user_id=ids[nickname], course_id=ids["course"], role=role
                )
            )
        conn.execute(insert(Administrator).values(email=ADMIN))
        ids["challenge"] = conn.execute(
            insert(Challenge)
            .values(
                title="Two Sum",
                description="...",
                course_id=ids["course"],
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
                max_threads=1,
            )
            .returning(Challenge.c.id)
        ).scalar_one()
        ids["submission"] = conn.execute(
            insert(Submission)
            .values(
                user_id=ids["bob"],
                challenge_id=ids["challenge"],
                status=SubmissionStatus.done,
                language="python",
                filename="sol.py",
                source="print(1)",
            )
            .returning(Submission.c.id)
        ).scalar_one()

    engine = create_async_engine(postgres_url)

    async def _get_db():
        async with engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db
    monkeypatch.setattr(main, "DUMMY_AUTH", True)
    monkeypatch.setattr(main, "DUMMY_USER", TEACHER)

    client = TestClient(main.app)
    yield client, sync_engine, ids
    main.app.dependency_overrides.clear()
    await engine.dispose()


def _roles_on(engine, course_id):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                select(User.c.email, Enrollment.c.role)
                .select_from(
                    Enrollment.join(User, User.c.id == Enrollment.c.user_id)
                )
                .where(Enrollment.c.course_id == course_id)
            ).all()
        )


@pytest.mark.integration
async def test_the_page_lists_the_enrolled_and_nobody_else(pg_client):
    client, _, ids = pg_client
    response = client.get(f"/courses/{ids['course']}/people")
    assert response.status_code == 200
    assert TEACHER in response.text
    assert STUDENT in response.text
    # Dana is on no course, and Matteo is staff here without an enrollment.
    # This is the class list, not everyone who may open the page.
    assert OUTSIDER not in response.text
    assert ADMIN not in response.text


@pytest.mark.integration
async def test_an_administrator_is_staff_on_every_course(pg_client, monkeypatch):
    """Matteo is enrolled on nothing. `effective_course_role` answers `teacher`
    for him anyway, so the page opens and its mutations work - and he still
    does not appear on the roster, which is the enrollment table."""
    import cocco.main as main

    client, engine, ids = pg_client
    monkeypatch.setattr(main, "DUMMY_USER", ADMIN)

    assert client.get(f"/courses/{ids['course']}/people").status_code == 200
    response = client.post(
        f"/courses/{ids['course']}/people/{ids['bob']}/role",
        data={"new_role": "collaborator"},
    )
    assert response.status_code == 200
    assert _roles_on(engine, ids["course"])[STUDENT] == UserRole.collaborator


@pytest.mark.integration
async def test_enrolling_an_address_that_has_never_signed_in(pg_client):
    """The reason this takes an address: a teacher types out a roster before
    term starts, and most of those people have no account yet."""
    client, engine, ids = pg_client
    # Normalized on the way in - this is one of the two places in the app where
    # a human types an address.
    response = client.post(
        f"/courses/{ids['course']}/people",
        data={"email": "  Eve@UNIPD.it ", "new_role": "collaborator"},
    )
    assert response.status_code == 200
    assert "eve@unipd.it is now a collaborator" in response.text

    with engine.connect() as conn:
        row = conn.execute(
            select(User.c.id).where(User.c.email == "eve@unipd.it")
        ).first()
    assert row is not None, "the account should have been created"
    assert _roles_on(engine, ids["course"])["eve@unipd.it"] == UserRole.collaborator


@pytest.mark.integration
async def test_enrolling_refuses_an_address_nobody_could_sign_in_with(pg_client):
    """A pre-enrolled student who could never sign in is a typo worth catching
    at the form, where somebody is standing who can fix it."""
    client, engine, ids = pg_client
    for bad in ("not-an-address", "eve@gmail.com", ""):
        response = client.post(
            f"/courses/{ids['course']}/people",
            data={"email": bad, "new_role": "student"},
        )
        # 200 with an alert, never a 4xx: htmx does not swap an error response.
        assert response.status_code == 200
        assert sorted(_roles_on(engine, ids["course"])) == [TEACHER, STUDENT]


@pytest.mark.integration
async def test_a_role_is_never_taken_from_the_form_as_text(pg_client):
    """The enum is the whitelist, the same rule the leaderboard criterion
    follows. `admin` is not a `UserRole`, so it is not a role."""
    client, engine, ids = pg_client
    response = client.post(
        f"/courses/{ids['course']}/people",
        data={"email": "eve@unipd.it", "new_role": "admin"},
    )
    assert response.status_code == 200
    assert "not a role" in response.text
    assert "eve@unipd.it" not in _roles_on(engine, ids["course"])


@pytest.mark.integration
async def test_enrolling_somebody_already_on_the_course_changes_their_role(pg_client):
    """One upsert behind both halves of the page: `enrollment` is keyed on
    (user_id, course_id), so this names the row the selector names."""
    client, engine, ids = pg_client
    response = client.post(
        f"/courses/{ids['course']}/people",
        data={"email": STUDENT, "new_role": "collaborator"},
    )
    assert response.status_code == 200
    assert _roles_on(engine, ids["course"])[STUDENT] == UserRole.collaborator


@pytest.mark.integration
async def test_the_role_selector_promotes_and_demotes(pg_client):
    client, engine, ids = pg_client
    response = client.post(
        f"/courses/{ids['course']}/people/{ids['bob']}/role",
        data={"new_role": "teacher"},
    )
    assert response.status_code == 200
    assert _roles_on(engine, ids["course"])[STUDENT] == UserRole.teacher

    # And back down again - which is only allowed because Alice is still one.
    response = client.post(
        f"/courses/{ids['course']}/people/{ids['bob']}/role",
        data={"new_role": "student"},
    )
    assert response.status_code == 200
    assert _roles_on(engine, ids["course"])[STUDENT] == UserRole.student


@pytest.mark.integration
async def test_removing_somebody_leaves_their_work_alone(pg_client):
    """A person is removed; their work is not retracted. `submission.user_id`
    has no tie to `enrollment`, and the standings' cohort is a union with
    everyone who submitted."""
    client, engine, ids = pg_client
    response = client.post(
        f"/courses/{ids['course']}/people/{ids['bob']}/remove", data={}
    )
    assert response.status_code == 200
    assert STUDENT not in _roles_on(engine, ids["course"])

    with engine.connect() as conn:
        row = conn.execute(
            select(Submission.c.id).where(Submission.c.id == ids["submission"])
        ).first()
    assert row is not None, "their submission should have survived"


@pytest.mark.integration
async def test_you_cannot_remove_yourself(pg_client):
    """It would shut the door on the page you are standing on."""
    client, engine, ids = pg_client
    response = client.post(
        f"/courses/{ids['course']}/people/{ids['alice']}/remove", data={}
    )
    assert response.status_code == 200
    assert "cannot remove yourself" in response.text
    assert _roles_on(engine, ids["course"])[TEACHER] == UserRole.teacher


@pytest.mark.integration
async def test_a_course_never_loses_its_last_teacher(pg_client, monkeypatch):
    """Neither by removal nor by demotion. Alice is the only teacher, so both
    are refused; Matteo, who administers the site, is the one asking - so this
    is not the self-removal guard answering."""
    import cocco.main as main

    client, engine, ids = pg_client
    monkeypatch.setattr(main, "DUMMY_USER", ADMIN)

    response = client.post(
        f"/courses/{ids['course']}/people/{ids['alice']}/remove", data={}
    )
    assert response.status_code == 200
    assert "last teacher" in response.text
    assert _roles_on(engine, ids["course"])[TEACHER] == UserRole.teacher

    response = client.post(
        f"/courses/{ids['course']}/people/{ids['alice']}/role",
        data={"new_role": "student"},
    )
    assert response.status_code == 200
    assert "last teacher" in response.text
    assert _roles_on(engine, ids["course"])[TEACHER] == UserRole.teacher

    # With a second teacher there is nothing to protect, and the demotion goes
    # through - the guard is about the last one, not about teachers.
    client.post(
        f"/courses/{ids['course']}/people/{ids['bob']}/role",
        data={"new_role": "teacher"},
    )
    response = client.post(
        f"/courses/{ids['course']}/people/{ids['alice']}/role",
        data={"new_role": "student"},
    )
    assert response.status_code == 200
    assert _roles_on(engine, ids["course"])[TEACHER] == UserRole.student


@pytest.mark.integration
async def test_a_mutation_keeps_the_filter(pg_client):
    """Acting under a filter leaves the filter in force - the same reason
    `post_course_enroll` carries `q`."""
    client, _, ids = pg_client
    response = client.post(
        f"/courses/{ids['course']}/people/{ids['bob']}/role",
        data={"new_role": "collaborator", "q": "bob"},
    )
    assert response.status_code == 200
    assert STUDENT in response.text
    # Alice does not match "bob", so the answer is still the filtered list.
    assert TEACHER not in response.text
