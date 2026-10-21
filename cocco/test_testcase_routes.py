"""Uploading a test case, and downloading a sample.

The two ends of a test case file as a *browser* sees them - the form course
staff put one in through, and the link a student gets one back out of. Both
cross the boundary this storage draws: what is uploaded and what is downloaded
is plaintext, and what sits on the disk between them is compressed.
"""

import gzip
import io

import pytest
from sqlalchemy import insert, select

from cocco.conftest import PUBLISHED, sign_in_as
from cocco.model import Challenge, Course, Enrollment, TestCase, User, UserRole
from cocco.storage import Storage

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
OUTSIDER = "charlie@unipd.it"

# Big enough that compressing it is visible in the assertions, and repetitive
# the way a real test case is.
BIG_INPUT = "".join(f"{i} {i * 2}\n" for i in range(20000))


@pytest.fixture
async def world(app_engine, tmp_path, monkeypatch):
    """One course, one challenge, and a storage root really on disk - the point
    of these tests is what lands in it."""
    from cocco.web import core

    storage = Storage(tmp_path / "storage")
    monkeypatch.setattr(core, "storage", storage)

    ids: dict = {"storage": storage}
    async with app_engine.connect() as conn:
        for email, nickname in (
            (TEACHER, "alice"), (STUDENT, "bob"), (OUTSIDER, "charlie"),
        ):
            ids[nickname] = (
                await conn.execute(
                    insert(User)
                    .values(
                        email=email, nickname=nickname,
                        image=b"", image_mime="image/svg+xml",
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
        for who, role in (("alice", UserRole.teacher), ("bob", UserRole.student)):
            await conn.execute(
                insert(Enrollment).values(
                    user_id=ids[who], course_id=ids["course"], role=role
                )
            )
        ids["challenge"] = (
            await conn.execute(
                insert(Challenge)
                .values(
                    title="Two Sum", description="...", course_id=ids["course"],
                    max_memory_bytes=1 << 20, max_time_s=1.0, max_threads=1,
                    visible_from=PUBLISHED,
                )
                .returning(Challenge.c.id)
            )
        ).scalar_one()
        await conn.commit()
    return ids


@pytest.fixture
def client(make_client, world):
    return make_client(as_=TEACHER)


def upload(client, world, *, name="in_0.txt", body=BIG_INPUT, is_sample=True):
    return client.post(
        f"/courses/{world['course']}/{world['challenge']}/testcases",
        files={
            "input_file": (name, io.BytesIO(body.encode()), "text/plain"),
            "expected_output_file": (
                name.replace("in", "out"), io.BytesIO(b"42\n"), "text/plain",
            ),
        },
        data={"is_sample": "true" if is_sample else "false"},
    )


async def _the_testcase(app_engine) -> dict:
    async with app_engine.connect() as conn:
        return dict((await conn.execute(select(TestCase))).mappings().one())


async def test_an_uploaded_test_case_lands_compressed(client, world, app_engine):
    assert upload(client, world).status_code == 200

    row = await _the_testcase(app_engine)
    storage: Storage = world["storage"]

    # The row says what it always said - the name a teacher uploaded, with no
    # suffix of storage's own - and the bytes under it are gzip.
    assert row["input_path"].endswith("/in_0.txt")
    with gzip.open(storage.resolve(row["input_path"]), "rt") as fp:
        assert fp.read() == BIG_INPUT
    assert storage.size(row["input_path"]) < len(BIG_INPUT) // 2
    # ...and it reads back as what went in, which is all any caller sees.
    assert storage.read(row["input_path"]) == BIG_INPUT


async def test_a_sample_downloads_as_plaintext(client, world, app_engine):
    upload(client, world)
    row = await _the_testcase(app_engine)

    sign_in_as(client, STUDENT)
    response = client.get(
        f"/challenges/{world['challenge']}/samples/{row['id']}/input"
    )
    assert response.status_code == 200
    # What the browser gets is the file the teacher uploaded, not the gzip
    # bytes it is stored as: this is served decompressed, never as a
    # `Content-Encoding` the client has to know to undo.
    assert "content-encoding" not in response.headers
    assert response.text == BIG_INPUT
    assert response.headers["content-type"].startswith("text/plain")
    assert "sample1_input.txt" in response.headers["content-disposition"]


async def test_the_expected_output_is_a_sample_too(client, world, app_engine):
    upload(client, world)
    row = await _the_testcase(app_engine)

    response = client.get(
        f"/challenges/{world['challenge']}/samples/{row['id']}/output"
    )
    assert response.status_code == 200
    assert response.text == "42\n"


async def test_a_hidden_test_case_is_not_downloadable(client, world, app_engine):
    """Unchanged by any of this, and worth pinning beside it: what decides is
    `is_sample`, not whether the file happens to be readable."""
    upload(client, world, is_sample=False)
    row = await _the_testcase(app_engine)

    sign_in_as(client, STUDENT)
    assert client.get(
        f"/challenges/{world['challenge']}/samples/{row['id']}/input"
    ).status_code == 404


async def test_a_sample_belongs_to_the_class_it_was_set_for(client, world, app_engine):
    upload(client, world)
    row = await _the_testcase(app_engine)

    sign_in_as(client, OUTSIDER)
    assert client.get(
        f"/challenges/{world['challenge']}/samples/{row['id']}/input"
    ).status_code == 403
