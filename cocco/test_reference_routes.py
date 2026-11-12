"""The reference solutions card: who may use its routes, and what each one does."""

import io

import pytest
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, update

from cocco.conftest import PUBLISHED, sign_in_as
from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    OutcomeEnum,
    ReferenceVisibility,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
    UserRole,
)

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
OTHER_TEACHER = "charlie@unipd.it"


@pytest.fixture
async def world(app_engine):
    ids: dict = {}
    async with app_engine.connect() as conn:
        for email, nickname in (
            (TEACHER, "alice"), (STUDENT, "bob"), (OTHER_TEACHER, "charlie"),
        ):
            ids[nickname] = (
                await conn.execute(
                    insert(User)
                    .values(email=email, nickname=nickname, image=b"",
                            image_mime="image/svg+xml")
                    .returning(User.c.id)
                )
            ).scalar_one()
        for name in ("course", "other"):
            ids[name] = (
                await conn.execute(
                    insert(Course).values(title=name, description="...")
                    .returning(Course.c.id)
                )
            ).scalar_one()
        for who, course, role in (
            ("alice", "course", UserRole.teacher),
            ("bob", "course", UserRole.student),
            ("charlie", "other", UserRole.teacher),
        ):
            await conn.execute(
                insert(Enrollment).values(
                    user_id=ids[who], course_id=ids[course], role=role
                )
            )
        ids["challenge"] = (
            await conn.execute(
                insert(Challenge)
                .values(title="Two Sum", description="...", course_id=ids["course"],
                        max_memory_bytes=1 << 20, max_time_s=1.0, max_threads=1,
                        visible_from=PUBLISHED)
                .returning(Challenge.c.id)
            )
        ).scalar_one()
        ids["student_submission"] = (
            await conn.execute(
                insert(Submission)
                .values(user_id=ids["bob"], challenge_id=ids["challenge"],
                        language="python", filename="a.py", source="1",
                        status=SubmissionStatus.done)
                .returning(Submission.c.id)
            )
        ).scalar_one()
        ids["reference"] = (
            await conn.execute(
                insert(Submission)
                .values(user_id=ids["alice"], challenge_id=ids["challenge"],
                        language="python", filename="ref.py", source="1",
                        status=SubmissionStatus.done, reference_label="Naive",
                        reference_visibility=ReferenceVisibility.hidden)
                .returning(Submission.c.id)
            )
        ).scalar_one()
        await conn.commit()
    return ids


@pytest.fixture
def client(make_client, world):
    return make_client(as_=TEACHER)


def _base(world):
    return f"/courses/{world['course']}/{world['challenge']}/references"


def _routes(world):
    base, ref = _base(world), world["reference"]
    return [
        ("get", f"/f/references/{world['course']}/{world['challenge']}", {}),
        ("post", base, {"files": {"solution": ("a.py", io.BytesIO(b"1"), "text/plain")}}),
        ("post", f"{base}/{ref}", {"data": {"label": "x"}}),
        ("post", f"{base}/{ref}/rerun", {}),
        ("post", f"{base}/{ref}/delete", {}),
    ]


@pytest.mark.parametrize("who", [STUDENT, OTHER_TEACHER])
async def test_every_route_refuses_non_staff(make_client, world, app_engine, who):
    client = make_client(as_=who)
    for method, url, kwargs in _routes(world):
        assert getattr(client, method)(url, **kwargs).status_code == 403, url
    async with app_engine.connect() as conn:
        rows = (await conn.execute(select(Submission.c.id))).all()
    assert len(rows) == 2


def _upload(client, world, name="sol.py", body=b"print(1)\n", label=None):
    data = {} if label is None else {"label": label}
    return client.post(
        _base(world),
        files={"solution": (name, io.BytesIO(body), "text/plain")},
        data=data,
    )


async def _reference_rows(app_engine):
    async with app_engine.connect() as conn:
        return (
            await conn.execute(
                select(Submission).where(Submission.c.reference_label.is_not(None))
                .order_by(Submission.c.id)
            )
        ).mappings().all()


async def test_add_enqueues_a_hidden_reference_and_notifies(
    client, world, app_engine, monkeypatch
):
    from cocco.repository import challenges

    calls = []
    real = challenges.notify_queue

    async def spy(conn):
        calls.append(1)
        await real(conn)

    monkeypatch.setattr(challenges, "notify_queue", spy)
    response = _upload(client, world, label="Teacher's")
    assert response.status_code == 200
    assert "queued" in response.text and "Teacher" in response.text
    assert calls

    new = (await _reference_rows(app_engine))[-1]
    assert new["reference_label"] == "Teacher's"
    assert new["reference_visibility"] == ReferenceVisibility.hidden
    assert new["status"] == SubmissionStatus.queued
    assert new["user_id"] == world["alice"]


async def test_a_blank_label_becomes_the_file_name(client, world, app_engine):
    assert _upload(client, world, label="   ").status_code == 200
    assert (await _reference_rows(app_engine))[-1]["reference_label"] == "sol.py"


async def test_a_bad_extension_is_an_alert_in_the_card(client, world, app_engine):
    response = _upload(client, world, name="notes.xyz")
    assert response.status_code == 200
    assert 'id="references"' in response.text
    assert "alert-error" in response.text and "Cannot tell the language" in response.text
    assert len(await _reference_rows(app_engine)) == 1


async def test_label_and_visibility_update(client, world, app_engine):
    url = f"{_base(world)}/{world['reference']}"
    assert client.post(url, data={"label": " Better "}).status_code == 200
    assert client.post(url, data={"visibility": "source"}).status_code == 200
    row = (await _reference_rows(app_engine))[0]
    assert row["reference_label"] == "Better"
    assert row["reference_visibility"] == ReferenceVisibility.source


async def test_a_blank_label_is_refused_and_nothing_changes(client, world, app_engine):
    response = client.post(f"{_base(world)}/{world['reference']}", data={"label": " "})
    assert "alert-error" in response.text
    assert (await _reference_rows(app_engine))[0]["reference_label"] == "Naive"


@pytest.mark.parametrize("action", ["rerun", "delete"])
async def test_rerun_and_delete_refuse_while_running(client, world, app_engine, action):
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Submission).where(Submission.c.id == world["reference"])
            .values(status=SubmissionStatus.running)
        )
        await conn.commit()
    response = client.post(f"{_base(world)}/{world['reference']}/{action}")
    assert response.status_code == 200 and "alert-error" in response.text
    rows = await _reference_rows(app_engine)
    assert len(rows) == 1 and rows[0]["status"] == SubmissionStatus.running


async def test_rerun_requeues_and_delete_removes(client, world, app_engine):
    base = f"{_base(world)}/{world['reference']}"
    response = client.post(f"{base}/rerun")
    assert "alert-success" in response.text
    assert (await _reference_rows(app_engine))[0]["status"] == SubmissionStatus.queued
    # a queued reference makes the card poll itself
    assert "hx-trigger" in response.text

    assert client.post(f"{base}/delete").status_code == 200
    assert await _reference_rows(app_engine) == []


async def test_an_id_that_is_not_a_reference_here_is_a_404(client, world):
    base = _base(world)
    for sid in (world["student_submission"], 9999):
        assert client.post(f"{base}/{sid}", data={"label": "x"}).status_code == 404
        assert client.post(f"{base}/{sid}/rerun").status_code == 404
        assert client.post(f"{base}/{sid}/delete").status_code == 404


async def test_the_editor_page_has_the_card(client, world):
    response = client.get(f"/courses/{world['course']}/{world['challenge']}/edit")
    assert response.status_code == 200
    assert "Reference solutions" in response.text and "Naive" in response.text


# ----- the submission page of a reference -----------------------------------


async def _set_visibility(app_engine, world, visibility):
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Submission).where(Submission.c.id == world["reference"])
            .values(reference_visibility=visibility)
        )
        await conn.commit()


async def _grade_on_a_hidden_case(app_engine, world):
    async with app_engine.connect() as conn:
        testcase = (
            await conn.execute(
                insert(TestCase)
                .values(challenge_id=world["challenge"], execution_order=0,
                        input_path="i", expected_output_path="o", is_sample=False)
                .returning(TestCase.c.id)
            )
        ).scalar_one()
        await conn.execute(
            insert(TestOutcome).values(
                submission_id=world["reference"], testcase_id=testcase,
                outcome=OutcomeEnum.success, elapsed_s=0.1, score=1,
                message="SECRET-EXPECTED", stdout="SECRET-STDOUT",
            )
        )
        await conn.commit()


def _urls(world):
    sid = world["reference"]
    return [f"/submissions/{sid}", f"/f/submission/{sid}"]


async def test_a_student_reads_a_source_reference(make_client, world, app_engine):
    await _set_visibility(app_engine, world, ReferenceVisibility.source)
    await _grade_on_a_hidden_case(app_engine, world)
    client = make_client(as_=STUDENT)
    page = client.get(f"/submissions/{world['reference']}")
    assert page.status_code == 200
    assert "Reference solution" in page.text and "Naive" in page.text
    assert "#None" not in page.text
    assert TEACHER not in page.text and "alice" not in page.text
    assert "#references" not in page.text
    fragment = client.get(f"/f/submission/{world['reference']}")
    assert fragment.status_code == 200
    assert "SECRET" not in page.text + fragment.text


async def test_staff_see_the_hidden_case_text_and_the_editor_link(
    make_client, world, app_engine
):
    await _set_visibility(app_engine, world, ReferenceVisibility.source)
    await _grade_on_a_hidden_case(app_engine, world)
    client = make_client(as_=TEACHER)
    page = client.get(f"/submissions/{world['reference']}").text
    assert f"/courses/{world['course']}/{world['challenge']}/edit#references" in page
    assert "SECRET-EXPECTED" in client.get(f"/f/submission/{world['reference']}").text


@pytest.mark.parametrize(
    "visibility", [ReferenceVisibility.results, ReferenceVisibility.hidden]
)
async def test_a_student_cannot_read_a_reference_not_shared_as_source(
    make_client, world, app_engine, visibility
):
    await _set_visibility(app_engine, world, visibility)
    client = make_client(as_=STUDENT)
    for url in _urls(world):
        assert client.get(url).status_code == 403, url


async def test_a_non_member_cannot_read_a_source_reference(
    make_client, world, app_engine
):
    await _set_visibility(app_engine, world, ReferenceVisibility.source)
    client = make_client(as_=OTHER_TEACHER)
    for url in _urls(world):
        assert client.get(url).status_code == 403, url


@pytest.mark.parametrize(
    "visible_from, status",
    [(None, 404), (datetime.now(timezone.utc) + timedelta(days=30), 403)],
)
async def test_a_source_reference_follows_the_challenges_visibility(
    make_client, world, app_engine, visible_from, status
):
    await _set_visibility(app_engine, world, ReferenceVisibility.source)
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Challenge).where(Challenge.c.id == world["challenge"])
            .values(visible_from=visible_from)
        )
        await conn.commit()
    client = make_client(as_=STUDENT)
    for url in _urls(world):
        assert client.get(url).status_code == status, url
    # staff are not shut out of their own draft
    sign_in_as(client, TEACHER)
    assert client.get(_urls(world)[0]).status_code == 200


async def test_a_demoted_author_does_not_keep_a_hidden_reference(
    make_client, world, app_engine
):
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Enrollment).where(Enrollment.c.user_id == world["alice"])
            .values(role=UserRole.student)
        )
        await conn.commit()
    client = make_client(as_=TEACHER)
    for url in _urls(world):
        assert client.get(url).status_code == 403, url


async def test_a_duplicate_file_name_is_an_alert_and_inserts_nothing(
    client, world, app_engine
):
    assert _upload(client, world, name="dijkstra.py").status_code == 200
    before = await _reference_rows(app_engine)
    response = _upload(client, world, name="dijkstra.py", body=b"print(2)\n")
    assert response.status_code == 200
    assert 'id="references"' in response.text
    assert "alert-error" in response.text
    assert "A reference named dijkstra.py already exists" in response.text
    assert len(await _reference_rows(app_engine)) == len(before)


@pytest.mark.parametrize("name", ["my solution.py", "résumé.py", ".hidden.py"])
async def test_an_unsafe_file_name_is_an_alert_and_inserts_nothing(
    client, world, app_engine, name
):
    before = await _reference_rows(app_engine)
    response = _upload(client, world, name=name)
    assert response.status_code == 200
    assert 'id="references"' in response.text
    assert "alert-error" in response.text
    assert "letters, digits, dot, dash and underscore only" in response.text
    assert len(await _reference_rows(app_engine)) == len(before)
