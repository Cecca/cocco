"""End-to-end tests of the transfer API, over an in-memory database.

Same harness as `test_challenge_routes.py` - `get_db` overridden with SQLite -
which works because everything these routes touch is plain Core: the page
queries with their `json_agg` are deliberately not on this path.

Nothing here touches `DUMMY_AUTH`, and it used to have to: the shortcut was the
first branch of `get_api_user` and never looked at a header, so a bearer token
presented to a development server was simply ignored - and the header is the
whole subject of this file. `get_api_user` has no such branch any more; the
development sign-in is a session like any other, and the session is already the
last way in here.
"""

import io
import zipfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from cocco.model import (
    Administrator,
    ApiToken,
    Challenge,
    Course,
    Enrollment,
    LeaderboardRanking,
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
from cocco.tokens import hash_token, mint_token
from cocco.transfer import MANIFEST_NAME, STATEMENT_NAME, dump_manifest, parse_manifest

TEACHER = "alice@unipd.it"
STUDENT = "bob@unipd.it"
OUTSIDER = "charlie@unipd.it"
ADMIN = "matteo.ceccarello@unipd.it"


@pytest.fixture
async def app_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def world(app_engine, tmp_path, monkeypatch):
    """One course Alice teaches and Bob studies, with a two-test-case challenge;
    a second course nobody is on, to be the thing staff of the first must not
    reach through. Charlie is on neither, Matteo administers the site.

    The test data really is on disk - `main.storage` is pointed at a tmp_path -
    because an export that did not read the files would prove nothing.
    """
    import cocco.main as main

    storage = Storage(tmp_path / "storage")
    monkeypatch.setattr(main, "storage", storage)

    ids: dict = {"storage": storage}
    async with app_engine.connect() as conn:
        for email, nickname in (
            (TEACHER, "alice"), (STUDENT, "bob"),
            (OUTSIDER, "charlie"), (ADMIN, "matteo"),
        ):
            ids[nickname] = (
                await conn.execute(
                    insert(User)
                    .values(email=email, nickname=nickname, image=b"",
                            image_mime="image/svg+xml")
                    .returning(User.c.id)
                )
            ).scalar_one()
        await conn.execute(insert(Administrator).values(email=ADMIN))

        for key, title in (("course", "Algorithms 101"), ("other", "Data Structures")):
            ids[key] = (
                await conn.execute(
                    insert(Course).values(title=title, description="...")
                    .returning(Course.c.id)
                )
            ).scalar_one()
        await conn.execute(insert(Enrollment).values(
            user_id=ids["alice"], course_id=ids["course"], role=UserRole.teacher))
        await conn.execute(insert(Enrollment).values(
            user_id=ids["bob"], course_id=ids["course"], role=UserRole.student))

        for key, course_key, title in (
            ("challenge", "course", "Two Sum"),
            ("other_challenge", "other", "BST insert"),
        ):
            ids[key] = (
                await conn.execute(
                    insert(Challenge)
                    .values(title=title, description="Find them.",
                            course_id=ids[course_key], max_memory_bytes=128 << 20,
                            max_time_s=2.5, max_threads=1,
                            ranking=LeaderboardRanking.total_score, position=0)
                    .returning(Challenge.c.id)
                )
            ).scalar_one()

        directory = f"challenge_{ids['challenge']}"
        for order, (name, sample) in enumerate(
            ((("01"), True), (("02"), False))
        ):
            storage.write(f"{directory}/{name}.in", f"input {name}\n")
            storage.write(f"{directory}/{name}.out", f"output {name}\n")
            ids[f"tc{name}"] = (
                await conn.execute(
                    insert(TestCase)
                    .values(challenge_id=ids["challenge"], execution_order=order,
                            input_path=f"{directory}/{name}.in",
                            expected_output_path=f"{directory}/{name}.out",
                            is_sample=sample, is_active=True)
                    .returning(TestCase.c.id)
                )
            ).scalar_one()

        for nickname in ("alice", "bob", "charlie", "matteo"):
            token = mint_token()
            ids[f"token_{nickname}"] = token
            await conn.execute(insert(ApiToken).values(
                user_id=ids[nickname], label="cli", token_hash=hash_token(token)))
        await conn.commit()
    return ids


@pytest.fixture
def client(app_engine):
    import cocco.main as main

    async def _get_db():
        async with app_engine.connect() as conn:
            yield conn

    main.app.dependency_overrides[main.get_db] = _get_db

    client = TestClient(main.app)
    yield client
    main.app.dependency_overrides.clear()


def auth(world, nickname: str) -> dict:
    return {"Authorization": f"Bearer {world[f'token_{nickname}']}"}


async def _grade(app_engine, world, testcase_id: int) -> None:
    """Score a submission against a test case, so it can no longer be deleted."""
    async with app_engine.connect() as conn:
        submission_id = (
            await conn.execute(
                insert(Submission)
                .values(user_id=world["bob"], challenge_id=world["challenge"],
                        status=SubmissionStatus.done, language="python",
                        filename="s.py", source="print()")
                .returning(Submission.c.id)
            )
        ).scalar_one()
        await conn.execute(insert(TestOutcome).values(
            submission_id=submission_id, testcase_id=testcase_id,
            outcome=OutcomeEnum.success, score=1.0))
        await conn.commit()


async def _testcases(app_engine, challenge_id: int):
    async with app_engine.connect() as conn:
        return (
            await conn.execute(
                select(TestCase)
                .where(TestCase.c.challenge_id == challenge_id)
                .order_by(TestCase.c.execution_order)
            )
        ).all()


def pack(members: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    return buffer.getvalue()


def push(client, world, nickname, challenge_id, members):
    return client.post(
        f"/api/challenges/{challenge_id}",
        headers=auth(world, nickname),
        files={"file": ("challenge.zip", pack(members), "application/zip")},
    )


def pull(client, world, nickname, challenge_id):
    response = client.get(
        f"/api/challenges/{challenge_id}", headers=auth(world, nickname)
    )
    return response, zipfile.ZipFile(io.BytesIO(response.content))


# ── authentication ──────────────────────────────────────────────────────────


async def test_no_token_is_a_401_and_not_a_redirect(client, world):
    """A CLI following the 303 `get_user` sends would be handed a login page
    with a 200 on it, and would report a challenge it never read."""
    response = client.get("/api/challenges", follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert "token" in response.json()["detail"].lower()


async def test_an_unknown_token_is_a_401(client, world):
    response = client.get(
        "/api/challenges", headers={"Authorization": f"Bearer {mint_token()}"}
    )
    assert response.status_code == 401


async def test_a_revoked_token_stops_working_immediately(client, world, app_engine):
    assert client.get("/api/challenges", headers=auth(world, "alice")).status_code == 200
    async with app_engine.connect() as conn:
        await conn.execute(
            ApiToken.delete().where(
                ApiToken.c.token_hash == hash_token(world["token_alice"])
            )
        )
        await conn.commit()
    assert client.get("/api/challenges", headers=auth(world, "alice")).status_code == 401


async def test_a_malformed_header_is_the_same_401(client, world):
    for header in ("Basic abc", world["token_alice"], "Bearer"):
        response = client.get("/api/challenges", headers={"Authorization": header})
        assert response.status_code == 401


async def test_a_blocked_account_is_refused_even_with_a_good_token(
    client, world, app_engine
):
    """The block is enforced wherever identity is resolved, or it would be
    absent from exactly the door that has no browser in front of it."""
    async with app_engine.connect() as conn:
        await conn.execute(
            update(User).where(User.c.id == world["alice"])
            .values(blocked_at=datetime(2026, 1, 1), blocked_reason="no")
        )
        await conn.commit()
    assert client.get("/api/challenges", headers=auth(world, "alice")).status_code == 403


async def test_using_a_token_records_it(client, world, app_engine):
    async with app_engine.connect() as conn:
        before = (await conn.execute(select(ApiToken.c.last_used_at).where(
            ApiToken.c.token_hash == hash_token(world["token_alice"])))).scalar_one()
    assert before is None
    client.get("/api/challenges", headers=auth(world, "alice"))
    async with app_engine.connect() as conn:
        after = (await conn.execute(select(ApiToken.c.last_used_at).where(
            ApiToken.c.token_hash == hash_token(world["token_alice"])))).scalar_one()
    assert after is not None


# ── authorization ───────────────────────────────────────────────────────────


async def test_a_student_cannot_pull_a_challenge(client, world):
    """A checker and the hidden expected outputs are exactly what a student
    must not have, so this is a 403 even though it is only a read."""
    response = client.get(
        f"/api/challenges/{world['challenge']}", headers=auth(world, "bob")
    )
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")


async def test_a_student_cannot_push(client, world):
    response = push(client, world, "bob", world["challenge"], {MANIFEST_NAME: ""})
    assert response.status_code == 403


async def test_staff_of_one_course_are_strangers_on_another(client, world):
    response = client.get(
        f"/api/challenges/{world['other_challenge']}", headers=auth(world, "alice")
    )
    assert response.status_code == 403


async def test_the_unenrolled_are_shut_out(client, world):
    response = client.get(
        f"/api/challenges/{world['challenge']}", headers=auth(world, "charlie")
    )
    assert response.status_code == 403


async def test_an_administrator_is_staff_on_every_course(client, world):
    """Not through a role - Matteo is enrolled nowhere. `effective_challenge_role`
    is the one place that substitution lives, and the API resolves through it."""
    for key in ("challenge", "other_challenge"):
        response = client.get(
            f"/api/challenges/{world[key]}", headers=auth(world, "matteo")
        )
        assert response.status_code == 200


async def test_the_listing_shows_only_what_you_can_edit(client, world):
    alice = client.get("/api/challenges", headers=auth(world, "alice")).json()
    assert [row["challenge_id"] for row in alice] == [world["challenge"]]

    # A student is enrolled, but not staff, so there is nothing to edit.
    assert client.get("/api/challenges", headers=auth(world, "bob")).json() == []

    matteo = client.get("/api/challenges", headers=auth(world, "matteo")).json()
    assert {row["challenge_id"] for row in matteo} == {
        world["challenge"], world["other_challenge"]
    }


async def test_the_listing_marks_archived_challenges(client, world, app_engine):
    """The *effective* state, so a challenge whose course was archived is
    marked as well as one archived on its own. It is a note beside the title
    for somebody about to push to last year's course, not a gate."""
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Course).where(Course.c.id == world["other"])
            .values(archived_at=datetime(2026, 1, 1))
        )
        await conn.commit()

    rows = {
        row["challenge_id"]: row
        for row in client.get("/api/challenges", headers=auth(world, "matteo")).json()
    }
    assert rows[world["challenge"]]["archived"] is False
    assert rows[world["other_challenge"]]["archived"] is True


async def test_a_push_to_an_archived_challenge_still_works(
    client, world, app_engine
):
    """Written down so it is not "fixed" later. Archiving is a visibility
    state, not a lock: staff go on editing an archived challenge in the
    browser, and the API is the editor by another route. A script the browser
    accepted and the CLI refused would be a difference nobody could explain.

    The manifest gains no `archived` key either, for the reason `position` has
    none - so a round trip through a directory cannot silently reopen a
    challenge that was closed on purpose.
    """
    async with app_engine.connect() as conn:
        await conn.execute(
            update(Challenge).where(Challenge.c.id == world["challenge"])
            .values(archived_at=datetime(2026, 1, 1))
        )
        await conn.commit()

    _, archive = pull(client, world, "alice", world["challenge"])
    assert "archived" not in archive.read(MANIFEST_NAME).decode()

    members = {name: archive.read(name) for name in archive.namelist()}
    members[STATEMENT_NAME] = b"# Fixed a typo\n"
    assert push(client, world, "alice", world["challenge"], members).status_code == 200

    async with app_engine.connect() as conn:
        row = (await conn.execute(select(Challenge).where(
            Challenge.c.id == world["challenge"]))).one()
    assert row.description == "# Fixed a typo\n"
    # ...and it is still archived afterwards.
    assert row.archived_at is not None


# ── the export ──────────────────────────────────────────────────────────────


async def test_an_export_carries_everything_needed_to_rebuild_it(client, world):
    response, archive = pull(client, world, "alice", world["challenge"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert sorted(archive.namelist()) == sorted([
        MANIFEST_NAME, STATEMENT_NAME,
        "tests/01.in", "tests/01.out", "tests/02.in", "tests/02.out",
    ])
    assert archive.read("tests/01.in").decode() == "input 01\n"
    assert archive.read(STATEMENT_NAME).decode() == "Find them."

    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    assert manifest.title == "Two Sum"
    assert manifest.ranking is LeaderboardRanking.total_score
    assert manifest.max_time_s == 2.5
    assert manifest.max_memory_mb == 128
    assert manifest.evaluator is None
    assert manifest.testcases[0].is_sample and not manifest.testcases[1].is_sample


async def test_a_challenge_whose_file_is_missing_says_so(client, world):
    """A row can outlive its file - a failed commit after a write, a storage
    root that moved, a restore without the filesystem. The export names the
    file rather than half-writing a download and then raising."""
    world["storage"].delete(f"challenge_{world['challenge']}/02.in")
    response = client.get(
        f"/api/challenges/{world['challenge']}", headers=auth(world, "alice")
    )
    assert response.status_code == 409
    assert "02.in" in response.json()["detail"]


async def test_a_retired_test_case_is_still_exported(client, world, app_engine):
    """Otherwise a pull-edit-push cycle would delete it, which is the one thing
    retiring exists to prevent."""
    async with app_engine.connect() as conn:
        await conn.execute(update(TestCase)
                           .where(TestCase.c.id == world["tc02"])
                           .values(is_active=False))
        await conn.commit()
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    assert [tc.is_active for tc in manifest.testcases] == [True, False]


# ── the round trip ──────────────────────────────────────────────────────────


async def test_pushing_back_an_untouched_export_changes_nothing(
    client, world, app_engine
):
    before = await _testcases(app_engine, world["challenge"])
    _, archive = pull(client, world, "alice", world["challenge"])
    members = {name: archive.read(name) for name in archive.namelist()}

    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 200, response.text
    assert response.json()["testcases"] == {
        "updated": 2, "added": 0, "retired": [], "deleted": []
    }
    after = await _testcases(app_engine, world["challenge"])
    assert [(r.id, r.input_path, r.is_sample, r.is_active) for r in before] == \
           [(r.id, r.input_path, r.is_sample, r.is_active) for r in after]


async def test_a_push_writes_the_statement_the_limits_and_the_title(
    client, world, app_engine
):
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {name: archive.read(name) for name in archive.namelist()}
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__, "title": "Three Sum",
                          "max_time_s": 9.0, "max_memory_mb": 64.0,
                          "ranking": LeaderboardRanking.running_time})
    )
    members[STATEMENT_NAME] = "# Now in markdown\n"

    assert push(client, world, "alice", world["challenge"], members).status_code == 200

    async with app_engine.connect() as conn:
        row = (await conn.execute(select(Challenge).where(
            Challenge.c.id == world["challenge"]))).one()
    assert row.title == "Three Sum"
    assert row.description == "# Now in markdown\n"
    assert row.max_time_s == 9.0
    assert row.max_memory_bytes == 64 << 20
    assert row.ranking is LeaderboardRanking.running_time


async def test_reordering_the_blocks_reorders_the_test_cases(
    client, world, app_engine
):
    """The whole ergonomic point: moving two lines in a text file is the
    reorder that the editor spends two arrow-presses on."""
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {name: archive.read(name) for name in archive.namelist()}
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__,
                          "testcases": tuple(reversed(manifest.testcases))})
    )

    assert push(client, world, "alice", world["challenge"], members).status_code == 200
    rows = await _testcases(app_engine, world["challenge"])
    assert [r.id for r in rows] == [world["tc02"], world["tc01"]]
    assert [r.execution_order for r in rows] == [0, 1]


async def test_a_new_test_case_is_added_with_its_flags(client, world, app_engine):
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {name: archive.read(name) for name in archive.namelist()}
    members["tests/03.in"] = "input 03\n"
    members["tests/03.out"] = "output 03\n"
    entry = type(manifest.testcases[0])(
        input="tests/03.in", expected_output="tests/03.out",
        is_sample=True, is_active=False,
    )
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__,
                          "testcases": manifest.testcases + (entry,)})
    )

    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 200, response.text
    assert response.json()["testcases"]["added"] == 1

    rows = await _testcases(app_engine, world["challenge"])
    assert len(rows) == 3
    # `insert_testcase` cannot express `is_active`, so this is the follow-up
    # `update_testcase` doing its job.
    assert (rows[2].is_sample, rows[2].is_active) == (True, False)
    assert world["storage"].read(rows[2].input_path) == "input 03\n"


async def test_an_ungraded_test_case_dropped_from_the_manifest_is_deleted(
    client, world, app_engine
):
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {
        name: archive.read(name) for name in archive.namelist()
        if not name.startswith("tests/02")
    }
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__,
                          "testcases": manifest.testcases[:1]})
    )

    response = push(client, world, "alice", world["challenge"], members)
    assert response.json()["testcases"]["deleted"] == ["02.in"]
    assert len(await _testcases(app_engine, world["challenge"])) == 1
    assert not world["storage"].exists(f"challenge_{world['challenge']}/02.in")


async def test_a_graded_test_case_dropped_from_the_manifest_is_retired(
    client, world, app_engine
):
    """The rule an import must not be able to break: detaching an outcome would
    leave a score in the history with nothing to say what produced it."""
    await _grade(app_engine, world, world["tc02"])

    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {
        name: archive.read(name) for name in archive.namelist()
        if not name.startswith("tests/02")
    }
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__,
                          "testcases": manifest.testcases[:1]})
    )

    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 200, response.text
    assert response.json()["testcases"] == {
        "updated": 1, "added": 0, "retired": ["02.in"], "deleted": []
    }

    rows = await _testcases(app_engine, world["challenge"])
    assert len(rows) == 2
    retired = next(r for r in rows if r.id == world["tc02"])
    assert retired.is_active is False
    # Row, files and outcome all still there.
    assert world["storage"].exists(retired.input_path)


async def test_a_name_a_retired_test_case_holds_cannot_be_taken(
    client, world, app_engine
):
    """A retired case keeps its two files, so a push that drops it and then
    hands one of its names to a different test case would write over the data
    the retirement exists to preserve. Refused before anything is stored."""
    await _grade(app_engine, world, world["tc02"])
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())

    # 02 leaves the manifest - so it is retired, files and all - while a brand
    # new test case claims `02.out` as its expected output.
    thief = type(manifest.testcases[0])(
        input="tests/03.in", expected_output="tests/02.out"
    )
    members = {
        name: archive.read(name) for name in archive.namelist()
        if name != "tests/02.in"
    }
    members["tests/03.in"] = "input 03\n"
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__,
                          "testcases": (manifest.testcases[0], thief)})
    )

    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 400
    assert "02.out" in response.json()["detail"]
    # And the graded case's expected output is untouched.
    assert world["storage"].read(
        f"challenge_{world['challenge']}/02.out"
    ) == "output 02\n"


# ── the evaluator ───────────────────────────────────────────────────────────


async def test_an_evaluator_is_exported_and_pushed_back(client, world, app_engine):
    source = "import sys, json\nprint(json.dumps({'score': 1.0}))\n"
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {name: archive.read(name) for name in archive.namelist()}
    members["evaluator/checker.py"] = source
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__, "evaluator": "evaluator/checker.py"})
    )

    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 200, response.text
    assert response.json()["evaluator"] == "checker.py"

    async with app_engine.connect() as conn:
        path = (await conn.execute(select(Challenge.c.evaluator_path).where(
            Challenge.c.id == world["challenge"]))).scalar_one()
    assert path == f"evaluator_{world['challenge']}/checker.py"
    assert world["storage"].read(path) == source

    # And it comes back out again.
    _, archive = pull(client, world, "alice", world["challenge"])
    assert archive.read("evaluator/checker.py").decode() == source


async def test_dropping_the_evaluator_key_goes_back_to_the_default(
    client, world, app_engine
):
    members_with = None
    source = "print('{\"score\": 1.0}')\n"
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {name: archive.read(name) for name in archive.namelist()}
    members["evaluator/checker.py"] = source
    members_with = dict(members)
    members_with[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__, "evaluator": "evaluator/checker.py"})
    )
    push(client, world, "alice", world["challenge"], members_with)

    # Now push without it.
    response = push(client, world, "alice", world["challenge"], {
        name: archive.read(name) for name in archive.namelist()
    })
    assert response.status_code == 200, response.text
    assert response.json()["evaluator"] is None
    async with app_engine.connect() as conn:
        path = (await conn.execute(select(Challenge.c.evaluator_path).where(
            Challenge.c.id == world["challenge"]))).scalar_one()
    assert path is None
    assert not world["storage"].exists(f"evaluator_{world['challenge']}/checker.py")


async def test_an_evaluator_in_a_language_the_judge_cannot_run_is_refused(
    client, world
):
    """The extension is the only thing that tells the judge how to run the
    script, so this is refused here rather than becoming an evaluator_error on
    every submission - the same rule the editor's upload applies."""
    _, archive = pull(client, world, "alice", world["challenge"])
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members = {name: archive.read(name) for name in archive.namelist()}
    members["evaluator/checker.brainfuck"] = "+++"
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__,
                          "evaluator": "evaluator/checker.brainfuck"})
    )
    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 400
    assert "language" in response.json()["detail"]


# ── refusals ────────────────────────────────────────────────────────────────


async def test_a_refusal_is_a_400_with_a_sentence_not_a_200_with_an_alert(
    client, world
):
    """The app's convention exists because htmx will not swap an error body.
    There is no htmx here, and a CLI that cannot tell a refusal from a success
    is one that overwrites a challenge and reports that it worked."""
    response = push(client, world, "alice", world["challenge"], {"nothing.txt": "x"})
    assert response.status_code == 400
    assert MANIFEST_NAME in response.json()["detail"]


async def test_something_that_is_not_a_zip_is_refused(client, world):
    response = client.post(
        f"/api/challenges/{world['challenge']}",
        headers=auth(world, "alice"),
        files={"file": ("challenge.zip", b"not a zip", "application/zip")},
    )
    assert response.status_code == 400


async def test_a_file_the_manifest_does_not_name_is_refused(client, world):
    """Ignoring a stray file silently is how somebody loses a test case they
    thought they had added."""
    _, archive = pull(client, world, "alice", world["challenge"])
    members = {name: archive.read(name) for name in archive.namelist()}
    members["tests/03.in"] = "orphan\n"
    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 400
    assert "03.in" in response.json()["detail"]


async def test_a_manifest_naming_a_missing_file_is_refused(client, world):
    _, archive = pull(client, world, "alice", world["challenge"])
    members = {
        name: archive.read(name) for name in archive.namelist()
        if name != "tests/02.out"
    }
    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 400
    assert "02.out" in response.json()["detail"]


async def test_a_bad_limit_is_refused_by_the_same_validator_the_forms_use(
    client, world
):
    _, archive = pull(client, world, "alice", world["challenge"])
    members = {name: archive.read(name) for name in archive.namelist()}
    members[MANIFEST_NAME] = archive.read(MANIFEST_NAME).decode().replace(
        "max_time_s = 2.5", "max_time_s = -1.0"
    )
    response = push(client, world, "alice", world["challenge"], members)
    assert response.status_code == 400
    assert "positive" in response.json()["detail"].lower()


async def test_nothing_is_written_when_a_push_is_refused(client, world, app_engine):
    """Every refusal happens before the first byte is stored, so a rejected
    push leaves the challenge exactly as it was."""
    before = await _testcases(app_engine, world["challenge"])
    _, archive = pull(client, world, "alice", world["challenge"])
    members = {name: archive.read(name) for name in archive.namelist()}
    members["evaluator/checker.brainfuck"] = "+++"
    manifest = parse_manifest(archive.read(MANIFEST_NAME).decode())
    members[MANIFEST_NAME] = dump_manifest(
        type(manifest)(**{**manifest.__dict__,
                          "evaluator": "evaluator/checker.brainfuck",
                          "testcases": manifest.testcases[:1]})
    )
    members.pop("tests/02.in")
    members.pop("tests/02.out")

    assert push(client, world, "alice", world["challenge"], members).status_code == 400
    after = await _testcases(app_engine, world["challenge"])
    assert [r.id for r in before] == [r.id for r in after]
    assert world["storage"].exists(f"challenge_{world['challenge']}/02.in")
