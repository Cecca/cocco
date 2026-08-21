"""End-to-end tests of creating a challenge and a course, over an in-memory
database.

Same harness as `test_auth_routes.py`, and for the same reason: `get_db` is
overridden with SQLite, which works because everything these routes touch -
`query_course`, `query_course_role`, `challenge_in_course`, `insert_challenge`,
`is_administrator`, `insert_course` - is plain Core. The course page itself is not
tested here, nor is the home page carrying the "New course" button: both run the
`json_agg` query only Postgres has.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.conftest import PUBLISHED
from cocco.model import (
    Administrator,
    Challenge,
    Course,
    Enrollment,
    User,
    UserRole,
    metadata,
)

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
# A `teacher` on the first course, exactly like Alice. What separates them is a
# row in `administrator`: it is what lets him create a course, and what makes
# him staff on the second one, which nobody enrolled him on.
ADMIN = "matteo.ceccarello@unipd.it"

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
    through, and its challenge is what they must not be able to edit - while
    matteo, who administers the site, must be able to reach both.
    """
    async with app_engine.connect() as conn:
        ids = {}
        for email, nickname in ((TEACHER, "alice"), (STUDENT, "bob"), (ADMIN, "matteo")):
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
        await conn.execute(
            insert(Enrollment).values(
                user_id=ids["matteo"], course_id=ids["course"], role=UserRole.teacher
            )
        )
        await conn.execute(insert(Administrator).values(email=ADMIN))
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
                    visible_from=PUBLISHED,
                )
                .returning(Challenge.c.id)
            )
        ).scalar_one()
        await conn.commit()
    return ids


@pytest.fixture
def client(app_engine, world, monkeypatch):
    # `world` is a dependency rather than an argument of the tests alone,
    # because signing in *creates* the account if it is missing: without the
    # ordering, the fixture would insert alice a moment before the seed does and
    # the seed would trip over its own unique index.
    import cocco.main as main

    async def _get_db():
        async with app_engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db
    # Signed in without a cookie and without Google. The user rows already
    # exist, so `get_or_create_user` finds them rather than making new ones.
    monkeypatch.setattr(main, "DUMMY_AUTH", True)

    # Not `with TestClient(...)`: entering the context runs the startup event,
    # which reaches for the real Postgres engine.
    client = TestClient(main.app)
    _sign_in_as(client, TEACHER)
    yield client
    main.app.dependency_overrides.clear()


def _sign_in_as(client, email: str):
    """Become somebody else. Dummy auth is a real session now, so this is a
    request rather than a monkeypatch, and `_sign_in` clears the old session
    before writing the new id - no identity leaks into the next one."""
    response = client.post("/auth/dummy", data={"email": email}, follow_redirects=False)
    assert response.status_code == 303, response.text


async def test_the_form_is_staff_only(client, world, monkeypatch):
    url = f"/courses/{world['course']}/challenges/new"

    assert client.get(url).status_code == 200

    _sign_in_as(client, STUDENT)
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
    _sign_in_as(client, STUDENT)

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


COURSE_FORM = {"title": "Graph Theory", "description": "# Graphs\n\nAll of them."}


async def test_creating_a_course_is_admin_only(client, world, monkeypatch):
    """Teaching a course is not the same as being allowed to make one.

    Alice is course staff and may do everything else in the editor; this is the
    one thing `EDITOR_ROLES` does not open.
    """
    _sign_in_as(client, ADMIN)
    assert client.get("/courses/new").status_code == 200

    for email in (TEACHER, STUDENT):
        _sign_in_as(client, email)
        assert client.get("/courses/new").status_code == 403
        # ...and hiding the button is not what stops them: the POST refuses too.
        assert client.post("/courses/new", data=COURSE_FORM).status_code == 403


async def test_a_created_course_lands_on_its_editor(
    client, world, app_engine, monkeypatch
):
    _sign_in_as(client, ADMIN)
    response = client.post("/courses/new", data=COURSE_FORM)

    # 204 + HX-Redirect, not a 302: htmx would splice a whole page into the
    # alert div this form targets.
    assert response.status_code == 204

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                select(Course).where(Course.c.title == "Graph Theory")
            )
        ).one()
        role = (
            await conn.execute(
                select(Enrollment.c.role).where(
                    (Enrollment.c.course_id == row.id)
                    & (Enrollment.c.user_id == world["matteo"])
                )
            )
        ).scalar_one()

    assert response.headers["HX-Redirect"].endswith(f"/courses/{row.id}/edit")
    assert row.description == "# Graphs\n\nAll of them."
    # Staff of the course they just made - otherwise that editor would 403 on
    # them. Being an admin elsewhere is not staff here.
    assert role == UserRole.teacher


async def test_a_new_course_needs_a_title(client, world, app_engine, monkeypatch):
    """200 with an alert, never a 4xx: htmx does not swap an error response."""
    _sign_in_as(client, ADMIN)
    response = client.post("/courses/new", data={**COURSE_FORM, "title": "   "})
    assert response.status_code == 200
    assert "A course needs a title." in response.text

    async with app_engine.connect() as conn:
        created = await conn.execute(
            select(Course.c.id).where(Course.c.title != "")
        )
    # Only the two the fixture made
    assert len(created.all()) == 2


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


async def test_an_administrator_is_staff_on_every_course(client, world, monkeypatch):
    """A site administrator is course staff everywhere, enrolled or not.

    Matteo has no enrollment at all on the second course, and `EDITOR_ROLES` is
    unchanged by his being an administrator - `effective_course_role` is what
    lets him in, so the difference has to show at the routes rather than only on
    "New course".
    """
    theirs, challenge = world["other"], world["other_challenge"]
    # The staff-only form, rather than the editors: those render the `json_agg`
    # queries only Postgres has, and a 403 would prove nothing about a 200.
    url = f"/courses/{theirs}/challenges/new"

    _sign_in_as(client, ADMIN)
    assert client.get(url).status_code == 200
    # 204 + HX-Redirect is the success path: he did not merely see the form, he
    # created a challenge on a course nobody enrolled him on.
    assert client.post(url, data=FORM).status_code == 204
    # The cross-check still applies to him: a challenge that is not under the
    # course he named is a 404, not something an administrator may reach anyway.
    assert client.get(f"/courses/{world['course']}/{challenge}/edit").status_code == 404

    # And the power did not leak into `EDITOR_ROLES` on the way past.
    _sign_in_as(client, TEACHER)
    assert client.get(url).status_code == 403


########################################################################
# Hiding a challenge, and scheduling when it opens.
#
# `challenge.visible_from` is NULL for a hidden challenge, a past instant for a
# live one and a future one for a challenge that opens by itself. The whole
# enforcement is `require_challenge_visible`, called from the two dependencies
# that already gate a challenge - so these tests are about what those two
# refuse, and about the form that writes the column.

# Far from now on purpose: SQLite's `CURRENT_TIMESTAMP` is a naive UTC string
# and SQLAlchemy stores an aware datetime as its own wall clock, so two moments
# seconds apart could compare either way. Years apart, they cannot.
SCHEDULED = datetime(2099, 1, 1, 9, 0, tzinfo=timezone.utc)


async def _add_challenge(app_engine, course_id: int, visible_from):
    async with app_engine.connect() as conn:
        challenge_id = (
            await conn.execute(
                insert(Challenge)
                .values(
                    title="Dijkstra",
                    description="Find the shortest route.",
                    course_id=course_id,
                    max_memory_bytes=1 << 20,
                    max_time_s=1.0,
                    max_threads=1,
                    visible_from=visible_from,
                )
                .returning(Challenge.c.id)
            )
        ).scalar_one()
        await conn.commit()
    return challenge_id


def _submit(client, course_id: int, challenge_id: int):
    return client.post(
        f"/courses/{course_id}/{challenge_id}/submissions",
        files={"solution": ("solution.py", b"print(1)", "text/plain")},
    )


async def test_a_hidden_challenge_does_not_exist_for_a_student(
    client, world, app_engine
):
    """A 404 and not a 403: a refusal that named the challenge would tell them
    there is one, which is the whole point of hiding it.

    The page itself is `query_challenge`, which is Postgres-only, so what is
    asked here is the gate in front of it - and the gate is what decides.
    """
    challenge_id = await _add_challenge(app_engine, world["course"], None)
    _sign_in_as(client, STUDENT)
    response = client.get(f"/courses/{world['course']}/{challenge_id}")
    assert response.status_code == 404
    assert "Dijkstra" not in response.text


async def test_a_scheduled_challenge_says_when_it_opens(client, world, app_engine):
    """The other refusal, and deliberately not a 404: the student has a card for
    this challenge on their course page saying it opens, so a dead link there
    would read as a bug."""
    challenge_id = await _add_challenge(app_engine, world["course"], SCHEDULED)
    _sign_in_as(client, STUDENT)
    response = client.get(f"/courses/{world['course']}/{challenge_id}")
    assert response.status_code == 403
    assert "opens on" in response.text
    assert "2099" in response.text
    # ...and the statement is not in the page that refuses it.
    assert "Find the shortest route" not in response.text


@pytest.mark.parametrize("visible_from, status", [(None, 404), (SCHEDULED, 403)])
async def test_an_unpublished_challenge_takes_no_submission(
    client, world, app_engine, visible_from, status
):
    """`post_submission` has no visibility check of its own, and needs none: the
    dependency runs before the body, so the upload is refused without a byte of
    it being read. Note this is a real status code and not archiving's "200 with
    an alert" - the request never reaches the route that would render one."""
    challenge_id = await _add_challenge(app_engine, world["course"], visible_from)
    _sign_in_as(client, STUDENT)
    assert _submit(client, world["course"], challenge_id).status_code == status


async def test_a_published_challenge_takes_one(client, world, app_engine):
    """The third state, so the refusals above are about visibility and not about
    the harness."""
    challenge_id = await _add_challenge(app_engine, world["course"], PUBLISHED)
    _sign_in_as(client, STUDENT)
    assert _submit(client, world["course"], challenge_id).status_code == 200


async def test_staff_reach_a_challenge_they_have_not_published(
    client, world, app_engine
):
    """Course staff pass whatever the state is - it would be a strange hiding
    place that hid a challenge from the people writing it - which is also how
    they try their own before the class gets it."""
    challenge_id = await _add_challenge(app_engine, world["course"], None)
    assert _submit(client, world["course"], challenge_id).status_code == 200


async def test_the_fragments_are_gated_too(client, world, app_engine):
    """The three routes whose URL carries only a challenge go through
    `require_challenge_member`, which asks the same question. They are polled,
    so they simply freeze - exactly as they do for a student unenrolled
    mid-session."""
    challenge_id = await _add_challenge(app_engine, world["course"], None)
    _sign_in_as(client, STUDENT)
    for url in (
        f"/f/submissions/{challenge_id}",
        f"/f/leaderboard/{challenge_id}",
    ):
        assert client.get(url).status_code == 404, url


async def test_a_new_challenge_starts_hidden(client, world, app_engine):
    """The form's radio group defaults to Hidden, and so does the route behind
    it: a challenge with no test cases and no evaluator has no business being on
    a student's course page."""
    course_id = world["course"]
    assert client.post(f"/courses/{course_id}/challenges/new", data=FORM).status_code == 204

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                select(Challenge).where(Challenge.c.course_id == course_id)
            )
        ).one()
    assert row.visible_from is None


async def test_the_form_publishes_and_schedules(client, world, app_engine):
    """One column behind three radios, and the round trip through all three."""
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, None)
    url = f"/courses/{course_id}/{challenge_id}/edit"

    async def stored():
        async with app_engine.connect() as conn:
            return (
                await conn.execute(
                    select(Challenge.c.visible_from).where(
                        Challenge.c.id == challenge_id
                    )
                )
            ).scalar_one()

    edit = {k: v for k, v in FORM.items() if k != "title"}

    response = client.post(url, data={**edit, "visibility": "now"})
    assert response.status_code == 200
    published_at = await stored()
    assert published_at is not None

    # Saving a live challenge again keeps the date it went live: the badge says
    # "visible since", and an edit to the statement is not a republication.
    assert client.post(url, data={**edit, "visibility": "now"}).status_code == 200
    assert await stored() == published_at

    response = client.post(
        url, data={**edit, "visibility": "scheduled", "visible_at": "2099-01-01T09:00"}
    )
    assert response.status_code == 200
    scheduled = await stored()
    assert scheduled is not None and scheduled.year == 2099

    assert client.post(url, data={**edit, "visibility": "hidden"}).status_code == 200
    assert await stored() is None


async def test_scheduling_without_a_date_is_an_alert(client, world, app_engine):
    """200 with an alert and not a 4xx, which is this app's rule for a form
    htmx submitted - and nothing is written."""
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, PUBLISHED)
    edit = {k: v for k, v in FORM.items() if k != "title"}

    response = client.post(
        f"/courses/{course_id}/{challenge_id}/edit",
        data={**edit, "visibility": "scheduled", "visible_at": "  "},
    )
    assert response.status_code == 200
    assert "Pick the date and time" in response.text

    async with app_engine.connect() as conn:
        still = (
            await conn.execute(
                select(Challenge.c.visible_from).where(Challenge.c.id == challenge_id)
            )
        ).scalar_one()
    assert still is not None
