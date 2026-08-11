"""End-to-end tests of creating a challenge, over an in-memory database.

Same harness as `test_auth_routes.py`, and for the same reason: `get_db` is
overridden with SQLite, which works because everything these routes touch -
`query_course`, `query_course_role`, `challenge_in_course`, `insert_challenge` -
is plain Core. The course page itself is not tested here: it runs the
`json_agg` query only Postgres has.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.model import Challenge, Course, Enrollment, User, UserRole, metadata

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"

# A valid form, so a test only has to say what it wants to be wrong about.
FORM = {
    "title": "Two Sum",
    "description": "Find the two numbers.",
    "max_time_s": "2.5",
    "max_memory_mb": "128",
    "max_threads": "1",
    "ranking": "total_score",
}


@pytest.fixture
async def app_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(app_engine):
    """Two courses: alice teaches the first, bob studies it, nobody the second.

    The second course is what a teacher of the first must not be able to reach
    through, and its challenge is what they must not be able to edit.
    """
    async with app_engine.connect() as conn:
        ids = {}
        for email, nickname in ((TEACHER, "alice"), (STUDENT, "bob")):
            ids[nickname] = (
                await conn.execute(
                    insert(User)
                    .values(email=email, nickname=nickname, image=b"", image_mime="image/svg+xml")
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
        ids["other_challenge"] = (
            await conn.execute(
                insert(Challenge)
                .values(
                    title="BST insert",
                    description="...",
                    course_id=ids["other"],
                    max_memory_bytes=1 << 20,
                    max_time_s=1.0,
                    max_threads=1,
                )
                .returning(Challenge.c.id)
            )
        ).scalar_one()
        await conn.commit()
    return ids


@pytest.fixture
def client(app_engine, monkeypatch):
    import cocco.main as main

    async def _get_db():
        async with app_engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db
    # Signed in without a cookie and without Google. The user rows already
    # exist, so `get_or_create_user` finds them rather than making new ones.
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


async def test_the_form_is_staff_only(client, world, monkeypatch):
    url = f"/courses/{world['course']}/challenges/new"

    assert client.get(url).status_code == 200

    _sign_in_as(monkeypatch, STUDENT)
    assert client.get(url).status_code == 403
    # ...and hiding the button is not what stops them: the POST refuses too.
    assert client.post(url, data=FORM).status_code == 403


async def test_the_form_starts_from_the_defaults(client, world):
    response = client.get(f"/courses/{world['course']}/challenges/new")
    assert response.status_code == 200
    assert "New challenge" in response.text
    assert 'name="title"' in response.text
    # The seed's limits, not something tighter that would kill a first run
    assert 'value="5.0"' in response.text
    assert 'value="256"' in response.text


async def test_a_created_challenge_lands_on_its_editor(client, world, app_engine):
    course_id = world["course"]
    response = client.post(f"/courses/{course_id}/challenges/new", data=FORM)

    # 204 + HX-Redirect, not a 302: htmx would splice a whole page into the
    # alert div this form targets.
    assert response.status_code == 204

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                select(Challenge).where(Challenge.c.course_id == course_id)
            )
        ).one()
    # ...pointing at the editor of the challenge just created
    assert response.headers["HX-Redirect"].endswith(
        f"/courses/{course_id}/{row.id}/edit"
    )
    assert row.title == "Two Sum"
    assert row.course_id == course_id
    assert row.max_time_s == 2.5
    assert row.max_memory_bytes == 128 * 1024 * 1024
    assert row.ranking.value == "total_score"
    # No checker of its own yet: the bundled equality.py grades it
    assert row.evaluator_path is None


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("title", "   ", "A challenge needs a title."),
        ("max_time_s", "0", "Limits must be positive."),
        ("max_memory_mb", "-1", "Limits must be positive."),
        ("ranking", "by_vibes", "Unknown leaderboard ranking."),
    ],
)
async def test_a_bad_form_comes_back_as_an_alert(
    client, world, app_engine, field, value, expected
):
    """200 with an alert, never a 4xx: htmx does not swap an error response."""
    response = client.post(
        f"/courses/{world['course']}/challenges/new", data={**FORM, field: value}
    )
    assert response.status_code == 200
    assert expected in response.text

    async with app_engine.connect() as conn:
        created = await conn.execute(
            select(Challenge.c.id).where(Challenge.c.course_id == world["course"])
        )
    assert created.first() is None


async def test_the_course_editor_is_staff_only(client, world, monkeypatch):
    """The course's own page is readable by anyone signed in; editing it is not.

    Only the routes that render nothing from the Postgres-only page query are
    asserted here - see the module docstring - which is exactly the refusal.
    """
    course_id = world["course"]
    _sign_in_as(monkeypatch, STUDENT)

    assert client.get(f"/courses/{course_id}/edit").status_code == 403
    assert (
        client.post(
            f"/courses/{course_id}/edit",
            data={"title": "Hijacked", "description": "mine now"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/courses/{course_id}/challenges/{world['other_challenge']}/move",
            data={"direction": "up"},
        ).status_code
        == 403
    )


async def test_the_course_editor_saves_the_description(client, world, app_engine):
    course_id = world["course"]
    response = client.post(
        f"/courses/{course_id}/edit",
        data={"title": "Algorithms 102", "description": "# Welcome\n\nRead this."},
    )
    # 200 with an alert, like every other htmx form in the app
    assert response.status_code == 200
    assert "Saved." in response.text

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(select(Course).where(Course.c.id == course_id))
        ).one()
    assert row.title == "Algorithms 102"
    assert row.description == "# Welcome\n\nRead this."


async def test_a_course_cannot_lose_its_title(client, world, app_engine):
    course_id = world["course"]
    response = client.post(
        f"/courses/{course_id}/edit", data={"title": "   ", "description": "..."}
    )
    assert response.status_code == 200
    assert "A course needs a title." in response.text

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(select(Course).where(Course.c.id == course_id))
        ).one()
    assert row.title == "Algorithms 101"


async def test_moving_a_challenge_of_another_course_is_a_404(client, world):
    """The arrows go through `require_challenge_editor` like every other
    mutation: naming your own course in front of someone else's challenge is a
    URL for a challenge that does not exist, not a way to reorder theirs."""
    response = client.post(
        f"/courses/{world['course']}/challenges/{world['other_challenge']}/move",
        data={"direction": "up"},
    )
    assert response.status_code == 404


async def test_the_course_in_the_url_has_to_own_the_challenge(client, world):
    """The editor takes the role from the course in the path, so a teacher of
    one course must not be able to name it in front of another's challenge."""
    mine, theirs = world["course"], world["other"]
    challenge = world["other_challenge"]

    assert client.get(f"/courses/{mine}/{challenge}/edit").status_code == 404
    # Not a way in through the real owner either: alice does not teach it.
    assert client.get(f"/courses/{theirs}/{challenge}/edit").status_code == 403
