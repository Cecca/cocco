"""End-to-end tests of the two danger zones: archiving and deleting a course
or a challenge.

Same harness as `test_challenge_routes.py`, and for the same reason: `get_db`
is overridden with SQLite, which works because everything these routes touch -
`query_course`, `query_challenge_config`, `query_course_archived_at`,
`query_effective_archived_at`, `query_challenge_file_paths`,
`lock_*_submissions`, `set_*_archived`, `delete_challenge`, `delete_course` -
is plain Core. The `FOR UPDATE` the two lock helpers add is simply omitted by
SQLAlchemy's SQLite dialect, so the *count* is what is tested here and the
locking itself is left to `test_repository.py`.

What is deliberately not here is proof that the cascade is in the right order:
SQLite does not enforce foreign keys unless `PRAGMA foreign_keys=ON`, so a
wrong order would pass. These tests assert the rows are gone; the integration
tests in `test_repository.py` assert that a real database let them go.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.conftest import PUBLISHED
from cocco.model import (
    Administrator,
    Challenge,
    Course,
    Enrollment,
    OutcomeEnum,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
    UserRole,
    metadata,
)
from cocco.storage import Storage

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
ADMIN = "matteo.ceccarello@unipd.it"

COURSE_TITLE = "Algorithms 101"
CHALLENGE_TITLE = "Shortest Route Planner"


@pytest.fixture
async def app_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(app_engine, storage):
    """One course with a challenge that has been graded, and a sibling course.

    The challenge carries the whole chain a delete has to walk - test case,
    submission, outcome, and two files on disk - so "leaves nothing behind" is
    a statement about all of it. The sibling course is there to be left alone.
    """
    ids = {}
    async with app_engine.connect() as conn:
        for email, nickname in (
            (TEACHER, "alice"),
            (STUDENT, "bob"),
            (ADMIN, "matteo"),
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

        for key, title in (("course", COURSE_TITLE), ("other", "Data Structures")):
            ids[key] = (
                await conn.execute(
                    insert(Course)
                    .values(title=title, description="...")
                    .returning(Course.c.id)
                )
            ).scalar_one()

        for nickname, role in (
            ("alice", UserRole.teacher),
            ("bob", UserRole.student),
        ):
            await conn.execute(
                insert(Enrollment).values(
                    user_id=ids[nickname], course_id=ids["course"], role=role
                )
            )
        await conn.execute(insert(Administrator).values(email=ADMIN))

        for key, course_key, title in (
            ("challenge", "course", CHALLENGE_TITLE),
            ("other_challenge", "other", "BST insert"),
        ):
            ids[key] = (
                await conn.execute(
                    insert(Challenge)
                    .values(
                        title=title,
                        description="...",
                        course_id=ids[course_key],
                        max_memory_bytes=1 << 20,
                        max_time_s=1.0,
                        max_threads=1,
                        visible_from=PUBLISHED,
                    )
                    .returning(Challenge.c.id)
                )
            ).scalar_one()

        # Two files on disk, and the row that points at them.
        ids["input_path"] = f"challenge_{ids['challenge']}/01.in"
        ids["output_path"] = f"challenge_{ids['challenge']}/01.out"
        ids["evaluator_path"] = f"evaluator_{ids['challenge']}/check.py"
        storage.write(ids["input_path"], "1 2\n")
        storage.write(ids["output_path"], "3\n")
        storage.write(ids["evaluator_path"], "print('{\"score\": 1.0}')\n")
        await conn.execute(
            Challenge.update()
            .where(Challenge.c.id == ids["challenge"])
            .values(evaluator_path=ids["evaluator_path"])
        )

        ids["testcase"] = (
            await conn.execute(
                insert(TestCase)
                .values(
                    challenge_id=ids["challenge"],
                    execution_order=0,
                    input_path=ids["input_path"],
                    expected_output_path=ids["output_path"],
                    is_sample=True,
                )
                .returning(TestCase.c.id)
            )
        ).scalar_one()
        ids["submission"] = (
            await conn.execute(
                insert(Submission)
                .values(
                    user_id=ids["bob"],
                    challenge_id=ids["challenge"],
                    status=SubmissionStatus.done,
                    language="python",
                    filename="a.py",
                    source="print(3)",
                )
                .returning(Submission.c.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(TestOutcome).values(
                submission_id=ids["submission"],
                testcase_id=ids["testcase"],
                outcome=OutcomeEnum.success,
                score=1.0,
            )
        )
        await conn.commit()
    return ids


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """A storage root of this test's own, so a delete that unlinks files does
    it under `tmp_path` rather than in the developer's real store."""
    import cocco.main as main

    replacement = Storage(tmp_path / "store")
    monkeypatch.setattr(main, "storage", replacement)
    return replacement


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
    monkeypatch.setattr(main, "DUMMY_AUTH", True)

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


async def _count(app_engine, table, *where):
    async with app_engine.connect() as conn:
        return (
            await conn.execute(select(func.count()).select_from(table).where(*where))
        ).scalar_one()


async def _archived_at(app_engine, table, row_id):
    async with app_engine.connect() as conn:
        return (
            await conn.execute(
                select(table.c.archived_at).where(table.c.id == row_id)
            )
        ).scalar_one()


########################################################################
# Who may press the buttons


async def test_deleting_a_course_is_administrators_only(client, world, monkeypatch):
    """`Admin` and deliberately not `CourseEditor`: what exists at all is the
    site's business, not one course's - so the course's own teacher is refused
    here even though she may edit everything inside it."""
    url = f"/courses/{world['course']}/delete"
    data = {"confirm": COURSE_TITLE}

    assert client.post(url, data=data).status_code == 403  # alice, the teacher
    _sign_in_as(client, STUDENT)
    assert client.post(url, data=data).status_code == 403

    _sign_in_as(client, ADMIN)
    assert client.post(url, data=data).status_code == 204


async def test_archiving_a_course_is_administrators_only(client, world, monkeypatch):
    url = f"/courses/{world['course']}/archive"

    assert client.post(url).status_code == 403  # alice, the teacher
    _sign_in_as(client, STUDENT)
    assert client.post(url).status_code == 403

    _sign_in_as(client, ADMIN)
    assert client.post(url).status_code == 204


async def test_deleting_a_challenge_is_course_staff(client, world, monkeypatch):
    """`ChallengeEditor`, which is already "administrators and course staff" -
    and which keeps the cross-check that a challenge belongs to the course
    named in front of it."""
    course, challenge = world["course"], world["challenge"]
    data = {"confirm": CHALLENGE_TITLE}

    _sign_in_as(client, STUDENT)
    assert client.post(f"/courses/{course}/{challenge}/delete", data=data).status_code == 403

    # Alice teaches this course but not the other one, and naming her own
    # course in front of its challenge is a 404 rather than a way in.
    _sign_in_as(client, TEACHER)
    other = world["other_challenge"]
    assert client.post(f"/courses/{course}/{other}/delete", data=data).status_code == 404
    assert client.post(f"/courses/{world['other']}/{other}/delete", data=data).status_code == 403

    assert client.post(f"/courses/{course}/{challenge}/delete", data=data).status_code == 204


async def test_an_administrator_may_delete_a_challenge_anywhere(
    client, world, monkeypatch, app_engine
):
    """`effective_course_role` promotes an administrator to `teacher`, so
    `ChallengeEditor` admits them without an enrollment - here, on a course
    nobody put them on."""
    _sign_in_as(client, ADMIN)
    response = client.post(
        f"/courses/{world['other']}/{world['other_challenge']}/delete",
        data={"confirm": "BST insert"},
    )
    assert response.status_code == 204
    assert await _count(app_engine, Challenge, Challenge.c.id == world["other_challenge"]) == 0


########################################################################
# The typed name


@pytest.mark.parametrize(
    "typed",
    [
        "",  # the field left empty
        "algorithms 101",  # the right letters, the wrong case
        "Algorithms 10",  # very nearly
        "Data Structures",  # somebody else's title
    ],
)
async def test_a_course_delete_checks_the_typed_name(
    client, world, monkeypatch, app_engine, typed
):
    """The dialog's script only *enables* the button; a disabled control is
    presentation, never the check. So the refusal has to come from the route,
    and it is a 200 with an alert because htmx does not swap error responses."""
    _sign_in_as(client, ADMIN)
    response = client.post(
        f"/courses/{world['course']}/delete", data={"confirm": typed}
    )
    assert response.status_code == 200
    assert "Type the course's title exactly" in response.text
    assert await _count(app_engine, Course, Course.c.id == world["course"]) == 1


async def test_a_challenge_delete_checks_the_typed_name(
    client, world, app_engine
):
    response = client.post(
        f"/courses/{world['course']}/{world['challenge']}/delete",
        data={"confirm": "Shortest Route"},
    )
    assert response.status_code == 200
    assert "Type the challenge's title exactly" in response.text
    assert await _count(app_engine, Challenge, Challenge.c.id == world["challenge"]) == 1


async def test_the_typed_name_is_trimmed_but_otherwise_exact(client, world):
    """Trimmed, because a title copied off the page above comes with
    whitespace; otherwise exact, for the reason the enrollment key is."""
    response = client.post(
        f"/courses/{world['course']}/{world['challenge']}/delete",
        data={"confirm": f"  {CHALLENGE_TITLE}\n"},
    )
    assert response.status_code == 204


########################################################################
# What a delete takes with it


async def test_a_deleted_challenge_leaves_nothing_behind(
    client, world, app_engine, storage
):
    response = client.post(
        f"/courses/{world['course']}/{world['challenge']}/delete",
        data={"confirm": CHALLENGE_TITLE},
    )
    assert response.status_code == 204
    # It lands on the course editor: the list the challenge has just left.
    assert response.headers["HX-Redirect"].endswith(f"/courses/{world['course']}/edit")

    assert await _count(app_engine, Challenge, Challenge.c.id == world["challenge"]) == 0
    assert await _count(app_engine, TestCase, TestCase.c.id == world["testcase"]) == 0
    assert await _count(app_engine, Submission, Submission.c.id == world["submission"]) == 0
    assert await _count(app_engine, TestOutcome) == 0

    # The files go too, and so do the two directories they were alone in.
    assert not storage.exists(world["input_path"])
    assert not storage.exists(world["output_path"])
    assert not storage.exists(world["evaluator_path"])
    assert not storage.resolve(f"challenge_{world['challenge']}").exists()
    assert not storage.resolve(f"evaluator_{world['challenge']}").exists()

    # And the course it was on is still there, with everybody still on it.
    assert await _count(app_engine, Course, Course.c.id == world["course"]) == 1
    assert await _count(app_engine, Enrollment, Enrollment.c.course_id == world["course"]) == 2


async def test_a_deleted_course_takes_its_challenges_and_enrollments(
    client, world, monkeypatch, app_engine, storage
):
    _sign_in_as(client, ADMIN)
    response = client.post(
        f"/courses/{world['course']}/delete", data={"confirm": COURSE_TITLE}
    )
    assert response.status_code == 204
    assert response.headers["HX-Redirect"].endswith("/")

    assert await _count(app_engine, Course, Course.c.id == world["course"]) == 0
    assert await _count(app_engine, Challenge, Challenge.c.id == world["challenge"]) == 0
    assert await _count(app_engine, Enrollment, Enrollment.c.course_id == world["course"]) == 0
    assert await _count(app_engine, Submission) == 0
    assert await _count(app_engine, TestOutcome) == 0
    assert not storage.exists(world["input_path"])

    # The sibling course is untouched - a delete names one course.
    assert await _count(app_engine, Course, Course.c.id == world["other"]) == 1
    assert await _count(app_engine, Challenge, Challenge.c.id == world["other_challenge"]) == 1

    # The people are not deleted with the course they were on: a course ending
    # is not an account ending.
    assert await _count(app_engine, User) == 3


async def test_a_delete_is_refused_while_a_judge_is_running(
    client, world, monkeypatch, app_engine
):
    """The guard `post_regrade` has, for a sharper reason: deleting a
    submission a judge is mid-run on makes its `insert_test_outcomes` violate a
    foreign key inside a transaction nothing catches, which takes the judge
    process down."""
    async with app_engine.connect() as conn:
        await conn.execute(
            Submission.update()
            .where(Submission.c.id == world["submission"])
            .values(status=SubmissionStatus.running)
        )
        await conn.commit()

    response = client.post(
        f"/courses/{world['course']}/{world['challenge']}/delete",
        data={"confirm": CHALLENGE_TITLE},
    )
    assert response.status_code == 200
    assert "A judge is running 1 submission" in response.text
    assert await _count(app_engine, Challenge, Challenge.c.id == world["challenge"]) == 1

    _sign_in_as(client, ADMIN)
    response = client.post(
        f"/courses/{world['course']}/delete", data={"confirm": COURSE_TITLE}
    )
    assert response.status_code == 200
    assert "A judge is running 1 submission" in response.text
    assert await _count(app_engine, Course, Course.c.id == world["course"]) == 1


########################################################################
# Archiving


async def test_archiving_a_course_is_a_round_trip(
    client, world, monkeypatch, app_engine
):
    _sign_in_as(client, ADMIN)
    course = world["course"]

    assert client.post(f"/courses/{course}/archive").status_code == 204
    assert await _archived_at(app_engine, Course, course) is not None

    # Unarchiving clears the column rather than writing a second one: an
    # archive that is over has no date.
    assert client.post(f"/courses/{course}/unarchive").status_code == 204
    assert await _archived_at(app_engine, Course, course) is None


async def test_archiving_a_challenge_is_a_round_trip(client, world, app_engine):
    course, challenge = world["course"], world["challenge"]

    assert client.post(f"/courses/{course}/{challenge}/archive").status_code == 204
    assert await _archived_at(app_engine, Challenge, challenge) is not None

    assert client.post(f"/courses/{course}/{challenge}/unarchive").status_code == 204
    assert await _archived_at(app_engine, Challenge, challenge) is None


async def test_archiving_destroys_nothing(client, world, monkeypatch, app_engine, storage):
    """The whole point of the button beside Delete: it is a visibility state."""
    _sign_in_as(client, ADMIN)
    client.post(f"/courses/{world['course']}/archive")

    assert await _count(app_engine, Challenge, Challenge.c.id == world["challenge"]) == 1
    assert await _count(app_engine, Submission) == 1
    assert await _count(app_engine, TestOutcome) == 1
    assert storage.exists(world["input_path"])


def _submit(client, course, challenge):
    return client.post(
        f"/courses/{course}/{challenge}/submissions",
        files={"solution": ("a.py", b"print(3)\n", "text/x-python")},
    )


async def test_an_archived_challenge_refuses_a_submission(
    client, world, monkeypatch, app_engine
):
    course, challenge = world["course"], world["challenge"]
    client.post(f"/courses/{course}/{challenge}/archive")

    _sign_in_as(client, STUDENT)
    response = _submit(client, course, challenge)
    # 200 with an alert, never a 4xx: htmx does not swap the body of an error
    # response, and the fragment is the whole point of the request.
    assert response.status_code == 200
    assert "This challenge is archived" in response.text
    assert await _count(app_engine, Submission) == 1  # the seeded one, and no more


async def test_a_challenge_under_an_archived_course_refuses_a_submission(
    client, world, monkeypatch, app_engine
):
    """The `or` that `_EFFECTIVE_ARCHIVED_SQL` spells: the challenge's own
    column is still NULL here."""
    course, challenge = world["course"], world["challenge"]
    _sign_in_as(client, ADMIN)
    client.post(f"/courses/{course}/archive")
    assert await _archived_at(app_engine, Challenge, challenge) is None

    _sign_in_as(client, STUDENT)
    response = _submit(client, course, challenge)
    assert response.status_code == 200
    assert "This challenge is archived" in response.text
    assert await _count(app_engine, Submission) == 1


async def test_a_live_challenge_still_takes_submissions(client, world, monkeypatch, app_engine):
    """The other half of the two tests above: the refusal is about archiving
    and not about the harness."""
    _sign_in_as(client, STUDENT)
    response = _submit(client, world["course"], world["challenge"])
    assert response.status_code == 200
    assert "was queued as a python submission" in response.text
    assert await _count(app_engine, Submission) == 2


async def test_an_archived_course_is_not_taking_new_students(
    client, world, monkeypatch, app_engine
):
    """The second of the two places archiving is enforced. Checked before the
    enrollment key, because an archived course refuses whether or not the key
    is right."""
    course = world["course"]
    _sign_in_as(client, ADMIN)
    client.post(f"/courses/{course}/archive")

    # Matteo teaches nothing here and has no enrollment row of his own; the
    # button is gone from every listing, so this is the stale tab or the typed
    # URL that the route exists to answer.
    response = client.post(f"/courses/{course}/enroll", data={"next": f"/courses/{course}"})
    assert response.status_code == 200
    assert "archived and is not taking new students" in response.text
    # Still two enrollments: alice and bob, and nobody added.
    assert await _count(app_engine, Enrollment, Enrollment.c.course_id == course) == 2
