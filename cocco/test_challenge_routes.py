"""End-to-end tests of creating a challenge and a course.

Alice teaches the first course and Bob studies it; the second has nobody on it,
which is what makes it the course Alice may not name in front of her own
challenge. Matteo administers the site, so he is staff on both without an
enrollment anywhere - `effective_course_role`'s promotion, from the outside.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, update

from cocco.conftest import PUBLISHED, sign_in_as
from cocco.model import (
    Administrator,
    Challenge,
    Course,
    Enrollment,
    User,
    UserRole,
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
def client(make_client, world):
    # `world` is a dependency rather than an argument of the tests alone,
    # because signing in *creates* the account if it is missing: without the
    # ordering, the fixture would insert alice a moment before the seed does and
    # the seed would trip over its own unique index.
    return make_client(as_=TEACHER)


async def test_the_form_is_staff_only(client, world, monkeypatch):
    url = f"/courses/{world['course']}/challenges/new"

    assert client.get(url).status_code == 200

    sign_in_as(client, STUDENT)
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
    sign_in_as(client, STUDENT)

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
    sign_in_as(client, ADMIN)
    assert client.get("/courses/new").status_code == 200

    for email in (TEACHER, STUDENT):
        sign_in_as(client, email)
        assert client.get("/courses/new").status_code == 403
        # ...and hiding the button is not what stops them: the POST refuses too.
        assert client.post("/courses/new", data=COURSE_FORM).status_code == 403


async def test_a_created_course_lands_on_its_editor(
    client, world, app_engine, monkeypatch
):
    sign_in_as(client, ADMIN)
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
    sign_in_as(client, ADMIN)
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

    sign_in_as(client, ADMIN)
    assert client.get(url).status_code == 200
    # 204 + HX-Redirect is the success path: he did not merely see the form, he
    # created a challenge on a course nobody enrolled him on.
    assert client.post(url, data=FORM).status_code == 204
    # The cross-check still applies to him: a challenge that is not under the
    # course he named is a 404, not something an administrator may reach anyway.
    assert client.get(f"/courses/{world['course']}/{challenge}/edit").status_code == 404

    # And the power did not leak into `EDITOR_ROLES` on the way past.
    sign_in_as(client, TEACHER)
    assert client.get(url).status_code == 403


########################################################################
# Hiding a challenge, and scheduling when it opens.
#
# `challenge.visible_from` is NULL for a hidden challenge, a past instant for a
# live one and a future one for a challenge that opens by itself. The whole
# enforcement is `require_challenge_visible`, called from the two dependencies
# that already gate a challenge - so these tests are about what those two
# refuse, and about the form that writes the column.

# Far from now on purpose. The comparison is made by the database's `now()` and
# the constant is written by this process, so anything close would be a race
# between two clocks. Decades away, it cannot be.
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
    sign_in_as(client, STUDENT)
    response = client.get(f"/courses/{world['course']}/{challenge_id}")
    assert response.status_code == 404
    assert "Dijkstra" not in response.text


async def test_a_scheduled_challenge_says_when_it_opens(client, world, app_engine):
    """The other refusal, and deliberately not a 404: the student has a card for
    this challenge on their course page saying it opens, so a dead link there
    would read as a bug."""
    challenge_id = await _add_challenge(app_engine, world["course"], SCHEDULED)
    sign_in_as(client, STUDENT)
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
    sign_in_as(client, STUDENT)
    assert _submit(client, world["course"], challenge_id).status_code == status


async def test_a_published_challenge_takes_one(client, world, app_engine):
    """The third state, so the refusals above are about visibility and not about
    the harness."""
    challenge_id = await _add_challenge(app_engine, world["course"], PUBLISHED)
    sign_in_as(client, STUDENT)
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
    sign_in_as(client, STUDENT)
    for url in (
        f"/f/submissions/{challenge_id}",
        f"/f/leaderboard/{challenge_id}",
    ):
        assert client.get(url).status_code == 404, url


async def test_a_new_challenge_starts_hidden(client, world, app_engine):
    """The create page has no visibility choice at all, and the route behind it
    writes Hidden: a challenge with no test cases and no evaluator has no
    business being on a student's course page. It is published from the editor."""
    course_id = world["course"]
    page = client.get(f"/courses/{course_id}/challenges/new").text
    assert 'name="visibility"' not in page
    assert 'name="visible_at"' not in page
    # ...and a stale tab that still posts one changes nothing.
    response = client.post(
        f"/courses/{course_id}/challenges/new", data={**FORM, "visibility": "now"}
    )
    assert response.status_code == 204

    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                select(Challenge).where(Challenge.c.course_id == course_id)
            )
        ).one()
    assert row.visible_from is None


async def _stored(app_engine, challenge_id: int):
    async with app_engine.connect() as conn:
        return (
            await conn.execute(select(Challenge).where(Challenge.c.id == challenge_id))
        ).one()


async def test_visibility_is_publish_schedule_hide(client, world, app_engine):
    """One column behind three actions, and the round trip through all of them."""
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, None)
    base = f"/courses/{course_id}/{challenge_id}"

    response = client.post(f"{base}/publish")
    assert response.status_code == 200
    assert 'id="challenge-visibility"' in response.text
    published_at = (await _stored(app_engine, challenge_id)).visible_from
    assert published_at is not None

    # Publishing a live challenge keeps the date it went live: the badge says
    # "visible since", and a stale tab is not a republication.
    assert client.post(f"{base}/publish").status_code == 200
    assert (await _stored(app_engine, challenge_id)).visible_from == published_at

    response = client.post(f"{base}/schedule", data={"visible_at": "2099-01-01T09:00"})
    assert response.status_code == 200
    scheduled = (await _stored(app_engine, challenge_id)).visible_from
    assert scheduled is not None and scheduled.year == 2099

    # A scheduled challenge is brought forward to now
    assert client.post(f"{base}/publish").status_code == 200
    brought = (await _stored(app_engine, challenge_id)).visible_from
    assert brought.year < 2099

    assert client.post(f"{base}/hide").status_code == 200
    assert (await _stored(app_engine, challenge_id)).visible_from is None


async def test_scheduling_without_a_date_is_an_alert(client, world, app_engine):
    """200 with an alert and not a 4xx, which is this app's rule for a form
    htmx submitted - and nothing is written."""
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, PUBLISHED)

    response = client.post(
        f"/courses/{course_id}/{challenge_id}/schedule", data={"visible_at": "  "}
    )
    assert response.status_code == 200
    assert "Pick the date and time" in response.text

    assert (await _stored(app_engine, challenge_id)).visible_from == PUBLISHED


async def test_each_card_writes_only_its_own_columns(client, world, app_engine):
    """The single form used to rewrite everything, so two tabs overwrote each
    other. Seed distinct values, post one card, and the rest must not move."""
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, SCHEDULED)
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Challenge)
            .where(Challenge.c.id == challenge_id)
            .values(score_threshold=4.0, max_threads=3, max_time_s=1.5)
        )
        await conn.commit()
    base = f"/courses/{course_id}/{challenge_id}"
    columns = (
        "description", "ranking", "score_threshold", "max_time_s",
        "max_memory_bytes", "max_threads", "visible_from", "title",
    )

    def snapshot(row):
        return {c: row._mapping[c] for c in columns}

    before = snapshot(await _stored(app_engine, challenge_id))

    posts = [
        ("edit/statement", {"description": "New text."}, {"description": "New text."}),
        (
            "edit/leaderboard",
            {"ranking": "total_score", "score_threshold": "9"},
            {"ranking": before["ranking"].__class__("total_score"), "score_threshold": 9.0},
        ),
        (
            "edit/limits",
            {"max_time_s": "3", "max_memory_mb": "64", "max_threads": "2"},
            {"max_time_s": 3.0, "max_memory_bytes": 64 << 20, "max_threads": 2},
        ),
        ("publish", {}, None),
    ]
    expected = dict(before)
    for path, data, changes in posts:
        assert client.post(f"{base}/{path}", data=data).status_code == 200
        after = snapshot(await _stored(app_engine, challenge_id))
        if changes is None:
            # Visibility: brought forward from 2099, everything else untouched
            assert after["visible_from"] != before["visible_from"]
            assert after["visible_from"].year < 2099
            expected["visible_from"] = after["visible_from"]
        else:
            expected.update(changes)
        assert after == expected, path

    # ...and hiding touches the one column it names
    assert client.post(f"{base}/hide").status_code == 200
    expected["visible_from"] = None
    assert snapshot(await _stored(app_engine, challenge_id)) == expected


@pytest.mark.parametrize("value", ["0", "-1"])
async def test_garbage_limits_come_back_as_an_alert_with_the_typed_values(
    client, world, app_engine, value
):
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, None)
    before = await _stored(app_engine, challenge_id)

    response = client.post(
        f"/courses/{course_id}/{challenge_id}/edit/limits",
        data={"max_time_s": value, "max_memory_mb": "77", "max_threads": "5"},
    )
    assert response.status_code == 200
    assert "Limits must be positive." in response.text
    # The refused card keeps what was typed, and says it is unsaved
    assert 'id="challenge-limits"' in response.text
    assert f'value="{float(value)}"' in response.text
    assert 'value="77"' in response.text
    assert 'value="5"' in response.text
    assert "data-dirty" in response.text

    after = await _stored(app_engine, challenge_id)
    assert (after.max_time_s, after.max_memory_bytes, after.max_threads) == (
        before.max_time_s, before.max_memory_bytes, before.max_threads
    )


CARD_ROUTES = [
    ("edit/statement", {"description": "x"}),
    ("edit/leaderboard", {"ranking": "total_score"}),
    ("edit/limits", {"max_time_s": "1", "max_memory_mb": "64", "max_threads": "1"}),
    ("publish", {}),
    ("schedule", {"visible_at": "2099-01-01T09:00"}),
    ("hide", {}),
]


@pytest.mark.parametrize("path,data", CARD_ROUTES)
async def test_every_card_route_is_staff_only(client, world, app_engine, path, data):
    """Hiding the controls is presentation: each route refuses on its own."""
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, PUBLISHED)

    sign_in_as(client, STUDENT)
    response = client.post(f"/courses/{course_id}/{challenge_id}/{path}", data=data)
    assert response.status_code == 403
    assert (await _stored(app_engine, challenge_id)).visible_from == PUBLISHED


@pytest.mark.parametrize("path,data", CARD_ROUTES)
async def test_a_card_route_needs_the_course_that_owns_the_challenge(
    client, world, app_engine, path, data
):
    """Alice teaches the first course: naming it in front of the other's
    challenge is a challenge that does not exist."""
    response = client.post(
        f"/courses/{world['course']}/{world['other_challenge']}/{path}", data=data
    )
    assert response.status_code == 404
    other = await _stored(app_engine, world["other_challenge"])
    assert other.visible_from == PUBLISHED
    assert other.description == "..."


async def test_the_edit_page_offers_the_actions_its_state_allows(
    client, world, app_engine
):
    course_id = world["course"]
    hidden = await _add_challenge(app_engine, course_id, None)
    live = await _add_challenge(app_engine, course_id, PUBLISHED)
    scheduled = await _add_challenge(app_engine, course_id, SCHEDULED)

    page = client.get(f"/courses/{course_id}/{hidden}/edit").text
    assert "Publish now" in page
    assert "Schedule" in page
    assert "/hide" not in page
    # No form posts to the old single-form route, which no longer exists
    assert f'hx-post="/courses/{course_id}/{hidden}/edit"' not in page
    assert client.post(
        f"/courses/{course_id}/{hidden}/edit", data=FORM
    ).status_code in (404, 405)

    page = client.get(f"/courses/{course_id}/{live}/edit").text
    assert "Publish now" not in page
    assert 'name="visible_at"' not in page
    assert f"/courses/{course_id}/{live}/hide" in page
    # Live: hiding takes it off students' pages, so it asks first
    assert "hx-confirm=\"Hide this challenge?" in page

    page = client.get(f"/courses/{course_id}/{scheduled}/edit").text
    assert "Publish now" in page
    assert f"/courses/{course_id}/{scheduled}/hide" in page
    assert "Hide this challenge?" not in page


########################################################################
# The score threshold: a defaulted form field, empty meaning "none".

NO_THRESHOLD_WARNING = "No score threshold: every graded submission counts as passed"


@pytest.mark.parametrize("value,expected", [("", None), ("  ", None), ("7", 7.0), ("-1.5", -1.5)])
async def test_the_editor_saves_a_score_threshold(
    client, world, app_engine, value, expected
):
    challenge_id = await _add_challenge(app_engine, world["course"], None)
    response = client.post(
        f"/courses/{world['course']}/{challenge_id}/edit/leaderboard",
        data={"ranking": "total_score", "score_threshold": value},
    )
    assert response.status_code == 200
    assert "Saved." in response.text
    async with app_engine.connect() as conn:
        stored = (
            await conn.execute(
                select(Challenge.c.score_threshold).where(Challenge.c.id == challenge_id)
            )
        ).scalar_one()
    assert stored == expected


async def test_a_form_without_the_field_means_no_threshold(client, world, app_engine):
    """A defaulted field: a missing one would be a 422 htmx drops."""
    response = client.post(f"/courses/{world['course']}/challenges/new", data=FORM)
    assert response.status_code == 204
    async with app_engine.connect() as conn:
        stored = (
            await conn.execute(
                select(Challenge.c.score_threshold).where(
                    Challenge.c.course_id == world["course"]
                )
            )
        ).scalar_one()
    assert stored is None


@pytest.mark.parametrize("value", ["lots", "nan", "inf", "-inf", "1,5"])
async def test_a_garbage_threshold_comes_back_as_an_alert(
    client, world, app_engine, value
):
    challenge_id = await _add_challenge(app_engine, world["course"], None)
    for url in (
        f"/courses/{world['course']}/challenges/new",
        f"/courses/{world['course']}/{challenge_id}/edit/leaderboard",
    ):
        response = client.post(url, data={**FORM, "score_threshold": value})
        assert response.status_code == 200
        assert "The score threshold must be a number." in response.text
    # The refused card keeps the typed value and is flagged unsaved
    assert f'value="{value}"' in response.text
    assert "data-dirty" in response.text
    async with app_engine.connect() as conn:
        n = (await conn.execute(select(Challenge.c.id).where(
            Challenge.c.course_id == world["course"]))).all()
    assert len(n) == 1


async def test_the_threshold_is_shown_on_re_edit_and_the_warning_goes(
    client, world, app_engine
):
    course_id = world["course"]
    assert NO_THRESHOLD_WARNING in client.get(
        f"/courses/{course_id}/challenges/new"
    ).text

    challenge_id = await _add_challenge(app_engine, course_id, None)
    url = f"/courses/{course_id}/{challenge_id}/edit"
    assert NO_THRESHOLD_WARNING in client.get(url).text

    response = client.post(f"{url}/leaderboard", data={**FORM, "score_threshold": "3.5"})
    # The card itself comes back re-rendered: the warning goes with the save,
    # not on the next page load.
    assert 'id="challenge-leaderboard"' in response.text
    assert 'value="3.5"' in response.text
    assert NO_THRESHOLD_WARNING not in response.text

    page = client.get(url).text
    assert 'name="score_threshold"' in page
    assert 'value="3.5"' in page
    assert NO_THRESHOLD_WARNING not in page
    assert "Average score" not in page


########################################################################
# Scores are raw sums, and a threshold decides who has "failed".


@pytest.mark.parametrize(
    "value,text",
    [
        (None, "—"), (0, "0"), (0.0, "0"), (7, "7"), (7.0, "7"), (7.5, "7.5"),
        (1 / 3, "0.33"), (-1.5, "-1.5"), (1.0, "1"), (12.345, "12.35"),
    ],
)
def test_render_score_prints_the_raw_number(value, text):
    from cocco.web.templating import render_score

    assert render_score(value) == text


async def _graded(app_engine, challenge_id, user_id, scores):
    """A finished submission by `user_id` whose tests scored `scores`."""
    from cocco.model import (
        OutcomeEnum, Submission, SubmissionStatus, TestCase, TestOutcome,
    )

    async with app_engine.connect() as conn:
        submission_id = (
            await conn.execute(
                insert(Submission)
                .values(
                    user_id=user_id, challenge_id=challenge_id,
                    status=SubmissionStatus.done, language="python",
                    filename="a.py", source="print(1)",
                )
                .returning(Submission.c.id)
            )
        ).scalar_one()
        for order, score in enumerate(scores):
            testcase_id = (
                await conn.execute(
                    insert(TestCase)
                    .values(challenge_id=challenge_id, execution_order=order,
                            input_path="i", expected_output_path="o")
                    .returning(TestCase.c.id)
                )
            ).scalar_one()
            await conn.execute(
                insert(TestOutcome).values(
                    submission_id=submission_id, testcase_id=testcase_id,
                    outcome=OutcomeEnum.success, elapsed_s=0.1, score=score,
                )
            )
        await conn.commit()
    return submission_id


async def _set_threshold(app_engine, challenge_id, threshold):
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Challenge).where(Challenge.c.id == challenge_id)
            .values(score_threshold=threshold)
        )
        await conn.commit()


async def test_a_failed_submission_is_badged_failed_everywhere(
    client, world, app_engine
):
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, PUBLISHED)
    await _set_threshold(app_engine, challenge_id, 3)
    submission_id = await _graded(app_engine, challenge_id, world["bob"], [1, 1])

    sign_in_as(client, STUDENT)
    history = client.get(f"/f/submissions/{challenge_id}").text
    assert "failed · 2" in history
    assert "/ 100" not in history
    assert "failed · 2" in client.get(f"/submissions/{submission_id}").text

    sign_in_as(client, TEACHER)
    standings = client.get(f"/f/standings/{challenge_id}?course_id={course_id}").text
    assert "failed · 2" in standings


async def test_a_cleared_submission_is_not_failed(client, world, app_engine):
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, PUBLISHED)
    await _set_threshold(app_engine, challenge_id, 2)
    submission_id = await _graded(app_engine, challenge_id, world["bob"], [1, 1])

    sign_in_as(client, STUDENT)
    for url in (f"/f/submissions/{challenge_id}", f"/submissions/{submission_id}"):
        page = client.get(url).text
        assert "failed" not in page
        assert "badge-success" in page
    sign_in_as(client, TEACHER)
    standings = client.get(f"/f/standings/{challenge_id}?course_id={course_id}").text
    assert "failed" not in standings


async def test_without_a_threshold_nothing_is_ever_failed(client, world, app_engine):
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, PUBLISHED)
    submission_id = await _graded(app_engine, challenge_id, world["bob"], [0, 0])

    sign_in_as(client, STUDENT)
    for url in (f"/f/submissions/{challenge_id}", f"/submissions/{submission_id}"):
        page = client.get(url).text
        assert "failed" not in page
        assert "badge-error" not in page
    assert "Reach" not in client.get(f"/courses/{course_id}/{challenge_id}").text


async def test_the_leaderboard_empty_state_names_the_threshold(
    client, world, app_engine
):
    challenge_id = await _add_challenge(app_engine, world["course"], PUBLISHED)
    sign_in_as(client, STUDENT)
    plain = client.get(f"/f/leaderboard/{challenge_id}").text
    assert "Nobody has reached" not in plain

    await _set_threshold(app_engine, challenge_id, 7.5)
    page = client.get(f"/f/leaderboard/{challenge_id}").text
    assert "Nobody has reached 7.5 points yet — be the first." in page


async def test_the_leaderboard_shows_the_raw_total(client, world, app_engine):
    challenge_id = await _add_challenge(app_engine, world["course"], PUBLISHED)
    await _set_threshold(app_engine, challenge_id, 2)
    await _graded(app_engine, challenge_id, world["bob"], [1, 1.5])
    sign_in_as(client, STUDENT)
    page = client.get(f"/f/leaderboard/{challenge_id}").text
    assert "2.5" in page
    assert "/ 100" not in page and "250" not in page


async def test_the_staff_view_warns_about_a_missing_threshold(
    client, world, app_engine
):
    course_id = world["course"]
    challenge_id = await _add_challenge(app_engine, course_id, PUBLISHED)
    url = f"/courses/{course_id}/{challenge_id}"
    sign_in_as(client, TEACHER)
    page = client.get(url).text
    assert NO_THRESHOLD_WARNING in page
    assert f"/courses/{course_id}/{challenge_id}/edit" in page

    await _set_threshold(app_engine, challenge_id, 4)
    page = client.get(url).text
    assert NO_THRESHOLD_WARNING not in page
    assert "threshold 4" in page
    assert "Reach 4 points to make the leaderboard." in page

    sign_in_as(client, STUDENT)
    assert "Reach 4 points to make the leaderboard." in client.get(url).text
