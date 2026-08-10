from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

import cocco.model as cm
from cocco.avatar import generate_identicon
from cocco.evaluator import EqualityEvaluator
from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    Submission,
    TestCase,
    TestOutcome,
    User,
    metadata,
)
from cocco.model import UserRole
from cocco.repository import (
    count_running_submissions,
    count_testcase_outcomes,
    pull_queued_submission,
    query_challenge,
    query_challenge_standings,
    query_challenge_testcases,
    query_sample_testcase,
    query_user_role,
    query_user_submissions,
    regrade_challenge,
)
from cocco.schema import SubmissionRunData


def _make_engine(db_url="sqlite:///:memory:"):
    engine = create_engine(db_url)
    metadata.create_all(engine)
    return engine


def _make_example_database(db_url="sqlite:///:memory:"):
    engine = _make_engine(db_url)
    now = datetime.now()

    with engine.begin() as conn:
        users = [
            ("root@example.com", "root"),
            ("alice@example.com", "alice"),
            ("bob@example.com", "bob"),
            ("carol@example.com", "carol"),
            ("dave@example.com", "dave"),
            ("erin@example.com", "erin"),
        ]
        user_ids = {}
        for email, nickname in users:
            image, image_mime = generate_identicon(nickname)
            user_ids[nickname] = conn.execute(
                insert(User)
                .values(
                    email=email,
                    nickname=nickname,
                    image=image,
                    image_mime=image_mime,
                )
                .returning(User.c.id)
            ).scalar_one()

        courses = [
            ("Algorithms 101", "intro to algorithms"),
            ("Data Structures", "trees, graphs, hashing"),
            ("Systems Programming", "C, memory, syscalls"),
        ]
        course_ids = {
            title: conn.execute(
                insert(Course)
                .values(title=title, description=description)
                .returning(Course.c.id)
            ).scalar_one()
            for title, description in courses
        }

        enrollments = [
            ("alice", "Algorithms 101", cm.UserRole.teacher),
            ("alice", "Data Structures", cm.UserRole.teacher),
            ("bob", "Systems Programming", cm.UserRole.teacher),
            ("carol", "Algorithms 101", cm.UserRole.student),
            ("carol", "Data Structures", cm.UserRole.student),
            ("dave", "Algorithms 101", cm.UserRole.student),
            ("dave", "Systems Programming", cm.UserRole.student),
            ("erin", "Systems Programming", cm.UserRole.student),
        ]
        for nickname, course_title, role in enrollments:
            conn.execute(
                insert(Enrollment).values(
                    user_id=user_ids[nickname],
                    course_id=course_ids[course_title],
                    role=role,
                )
            )

        challenges = [
            ("Hello", "say hi", "Algorithms 101", 1 << 20, 1.0, 1),
            ("Sum", "sum two numbers", "Algorithms 101", 1 << 20, 1.0, 1),
            ("BST insert", "insert into a BST", "Data Structures", 1 << 21, 2.0, 1),
            ("Buffer copy", "copy bytes via syscalls", "Systems Programming", 1 << 22, 2.0, 1),
            ("Parallel sum", "sum with threads", "Systems Programming", 1 << 22, 3.0, 4),
        ]
        challenge_ids = {
            title: conn.execute(
                insert(Challenge)
                .values(
                    title=title,
                    description=description,
                    course_id=course_ids[course_title],
                    max_memory_bytes=mem,
                    max_time_s=time_s,
                    max_threads=threads,
                    evaluator=EqualityEvaluator(),
                )
                .returning(Challenge.c.id)
            ).scalar_one()
            for title, description, course_title, mem, time_s, threads in challenges
        }

        testcase_ids: dict[str, list[int]] = {}
        for challenge_title in challenge_ids:
            ids = []
            for order in range(3):
                tc_id = conn.execute(
                    insert(TestCase)
                    .values(
                        challenge_id=challenge_ids[challenge_title],
                        execution_order=order,
                        input_path=f"{challenge_title}/in_{order}.txt",
                        expected_output_path=f"{challenge_title}/out_{order}.txt",
                        # the last one is public but retired, so the "retired
                        # wins over public" rule is exercised
                        is_sample=order in (0, 2),
                        is_active=order != 2,
                    )
                    .returning(TestCase.c.id)
                ).scalar_one()
                ids.append(tc_id)
            testcase_ids[challenge_title] = ids

        submissions = [
            ("carol", "Hello", "python", "print('hi')",
             cm.SubmissionStatus.done, now - timedelta(minutes=30)),
            ("carol", "Hello", "c", "print('there')",
             cm.SubmissionStatus.done, now - timedelta(minutes=35)),
            ("carol", "Sum", "python", "a,b=map(int,input().split());print(a+b)",
             cm.SubmissionStatus.done, now - timedelta(minutes=20)),
            ("carol", "BST insert", "cpp", "// not finished",
             cm.SubmissionStatus.queued, now - timedelta(minutes=2)),
            ("dave", "Hello", "c", "// broken", cm.SubmissionStatus.done,
             now - timedelta(minutes=25)),
            ("dave", "Buffer copy", "c", "int main(){return 0;}",
             cm.SubmissionStatus.running, now - timedelta(minutes=5)),
            ("dave", "Parallel sum", "cpp", "// pending",
             cm.SubmissionStatus.queued, now - timedelta(minutes=1)),
            ("erin", "Buffer copy", "c", "int main(){return 0;}",
             cm.SubmissionStatus.queued, now - timedelta(seconds=30)),
            ("erin", "Parallel sum", "cpp", "// pending",
             cm.SubmissionStatus.queued, now - timedelta(seconds=10)),
        ]
        submission_ids = []
        for nickname, challenge_title, language, source, status, enqueued in submissions:
            sub_id = conn.execute(
                insert(Submission)
                .values(
                    user_id=user_ids[nickname],
                    challenge_id=challenge_ids[challenge_title],
                    language=language,
                    filename=f"solution.{language}",
                    source=source,
                    status=status,
                    enqueued=enqueued,
                )
                .returning(Submission.c.id)
            ).scalar_one()
            submission_ids.append((sub_id, challenge_title, status))

        outcome_cycle = [
            (cm.OutcomeEnum.success, 1.0),
            (cm.OutcomeEnum.success, 1.0),
            (cm.OutcomeEnum.runtime_error, 0.0),
            (cm.OutcomeEnum.timeout, 0.0),
            (cm.OutcomeEnum.compile_failure, 0.0),
        ]
        cursor = 0
        for sub_id, challenge_title, status in submission_ids:
            if status != cm.SubmissionStatus.done:
                continue
            for tc_id in testcase_ids[challenge_title]:
                outcome, score = outcome_cycle[cursor % len(outcome_cycle)]
                cursor += 1
                conn.execute(
                    insert(TestOutcome).values(
                        submission_id=sub_id,
                        testcase_id=tc_id,
                        outcome=outcome,
                        elapsed_s=0.05 * (cursor + 1),
                        peak_memory_bytes=512 * 1024 * (cursor + 1),
                        score=score,
                        message="ok" if outcome == cm.OutcomeEnum.success else "fail",
                        stdout="hello\n" if outcome == cm.OutcomeEnum.success else "",
                        stderr="" if outcome == cm.OutcomeEnum.success else "boom",
                    )
                )

    return engine


async def test_get_queued_submission_returns_oldest_queued():
    engine = _make_engine()
    now = datetime.now()

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@example.com",
                nickname="the-admin",
                image=image,
                image_mime=image_mime,
            )
            .returning(User.c.id)
        ).scalar_one()
        challenge_id = conn.execute(
            insert(Challenge)
            .values(
                title="Hello",
                description="say hi",
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
                evaluator=EqualityEvaluator(),
            )
            .returning(Challenge.c.id)
        ).scalar_one()

        oldest_id = conn.execute(
            insert(Submission)
            .values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('a')",
                status=cm.SubmissionStatus.queued,
                enqueued=now - timedelta(seconds=10),
            )
            .returning(Submission.c.id)
        ).scalar_one()

        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('b')",
                status=cm.SubmissionStatus.queued,
                enqueued=now,
            )
        )
        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('c')",
                status=cm.SubmissionStatus.running,
                enqueued=now - timedelta(seconds=20),
            )
        )

    with engine.connect() as conn:
        result: SubmissionRunData | None = await pull_queued_submission(conn)

    assert result is not None
    assert result.submission_id == oldest_id


async def test_queued_submission_skips_retired_testcases():
    """Retiring a test case is what stops it grading new submissions."""
    engine = _make_engine()

    with engine.begin() as conn:
        image, image_mime = generate_identicon("the-admin")
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@example.com",
                nickname="the-admin",
                image=image,
                image_mime=image_mime,
            )
            .returning(User.c.id)
        ).scalar_one()
        challenge_id = conn.execute(
            insert(Challenge)
            .values(
                title="Hello",
                description="say hi",
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
                evaluator=EqualityEvaluator(),
            )
            .returning(Challenge.c.id)
        ).scalar_one()

        for order, is_active in enumerate([True, False, True]):
            conn.execute(
                insert(TestCase).values(
                    challenge_id=challenge_id,
                    execution_order=order,
                    input_path=f"in_{order}.txt",
                    expected_output_path=f"out_{order}.txt",
                    is_active=is_active,
                )
            )

        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
                filename="solution.py",
                source="print('a')",
                status=cm.SubmissionStatus.queued,
                enqueued=datetime.now(),
            )
        )

    with engine.connect() as conn:
        result = await pull_queued_submission(conn)

    assert result is not None
    assert [t.execution_order for t in result.test_cases] == [0, 2]


async def test_get_queued_submission_returns_none_when_empty():
    engine = _make_engine()
    with engine.connect() as conn:
        result = await pull_queued_submission(conn)
    assert result is None


# The json_agg / row_to_json queries backing the web pages are Postgres-only,
# so this needs a live server (see PGUSER/PGDATABASE/PGHOST in the devShell).
@pytest.mark.integration
async def test_challenge_page_queries():
    import os

    url = (
        f"postgresql+psycopg://{os.environ['PGUSER']}@/"
        f"{os.environ['PGDATABASE']}?host={os.environ['PGHOST']}"
    )
    sync_engine = create_engine(url)
    metadata.drop_all(sync_engine)
    _make_example_database(url)

    engine = create_async_engine(url)
    async with engine.connect() as conn:
        # carol (user 4) is a student on Algorithms 101
        challenge = await query_challenge(conn, user_id=4, challenge_id=1)
        assert challenge is not None
        assert challenge["title"] == "Hello"
        assert challenge["course_title"] == "Algorithms 101"
        assert challenge["position"] == 1
        assert challenge["viewer_role"] == "student"
        # Two of the three are marked public, but the third is retired, and a
        # retired case is not offered to students however it is flagged.
        assert [s["execution_order"] for s in challenge["samples"]] == [0]

        # alice (user 2) teaches it, and is the one who gets the Edit button
        as_teacher = await query_challenge(conn, user_id=2, challenge_id=1)
        assert as_teacher is not None
        assert as_teacher["viewer_role"] == "teacher"

        # carol (user 4) has two attempts on "Hello", newest first
        submissions = await query_user_submissions(conn, user_id=4, challenge_id=1)
        assert submissions is not None
        assert [s["attempt"] for s in submissions] == [2, 1]
        assert [s["filename"] for s in submissions] == ["solution.python", "solution.c"]
        assert sum(1 for s in submissions if s["is_best"]) == 1

        # a challenge nobody has attempted still comes back, with no samples lost
        untouched = await query_challenge(conn, user_id=4, challenge_id=5)
        assert untouched is not None
        assert untouched["title"] == "Parallel sum"
        # carol is not enrolled on Systems Programming
        assert untouched["viewer_role"] is None

        # ...and its submission list is simply empty rather than None-ish
        assert await query_user_submissions(conn, user_id=4, challenge_id=5) is None

        # The editor's authorization check, and the list it drives
        assert await query_user_role(conn, user_id=2, challenge_id=1) == UserRole.teacher
        assert await query_user_role(conn, user_id=4, challenge_id=1) == UserRole.student
        assert await query_user_role(conn, user_id=4, challenge_id=5) is None

        # The editor lists retired cases too - that is how one is reinstated
        testcases = await query_challenge_testcases(conn, 1)
        assert [tc.execution_order for tc in testcases] == [0, 1, 2]
        assert [tc.is_sample for tc in testcases] == [True, False, True]
        assert [tc.is_active for tc in testcases] == [True, True, False]

        # ...but the retired one is not downloadable despite is_sample
        assert await query_sample_testcase(conn, 1, testcases[0].id) is not None
        assert await query_sample_testcase(conn, 1, testcases[2].id) is None

        # Every "Hello" submission was graded on all three, so none of them can
        # be deleted; a case on an unattempted challenge still can.
        assert [tc.n_outcomes for tc in testcases] == [3, 3, 3]
        assert await count_testcase_outcomes(conn, testcases[0].id) == 3

        untouched_cases = await query_challenge_testcases(conn, 5)
        assert [tc.n_outcomes for tc in untouched_cases] == [0, 0, 0]
        assert await count_testcase_outcomes(conn, untouched_cases[0].id) == 0

        # ── the staff view ────────────────────────────────────────────────
        # One row per student, best attempt first. alice only teaches the
        # course, so she is not part of the cohort; carol and dave are.
        standings = await query_challenge_standings(conn, 1)
        assert standings is not None
        assert [s["nickname"] for s in standings] == ["carol", "dave"]
        carol, dave = standings
        assert [carol["rank"], dave["rank"]] == [1, 2]
        # Both peak at 2 of 3 tests; carol is ahead on the tie-break, time.
        assert carol["score"] == pytest.approx(2 / 3)
        assert dave["score"] == pytest.approx(2 / 3)
        assert carol["elapsed_s"] < dave["elapsed_s"]
        assert [carol["n_passed"], carol["n_tests"]] == [2, 3]
        # Two attempts, but the row describes the better of them
        assert [carol["n_attempts"], dave["n_attempts"]] == [2, 1]
        assert [carol["n_pending"], dave["n_pending"]] == [0, 0]

        # An enrolled student who has handed in nothing still gets a row: who is
        # missing is the first thing a teacher looks for.
        sum_board = await query_challenge_standings(conn, 2)
        assert sum_board is not None
        idle = next(s for s in sum_board if s["nickname"] == "dave")
        assert idle["n_attempts"] == 0
        assert idle["score"] is None
        assert idle["last_enqueued"] is None

        # A queued attempt is pending and keeps a *null* score, rather than
        # being ranked as a zero it has not earned
        pending = await query_challenge_standings(conn, 5)
        assert pending is not None
        assert sorted(s["nickname"] for s in pending) == ["dave", "erin"]
        assert all(s["n_pending"] == 1 and s["score"] is None for s in pending)

        # ── regrading ─────────────────────────────────────────────────────
        # Nothing is running on "Hello", but dave's "Buffer copy" is - that is
        # what makes the route refuse.
        assert await count_running_submissions(conn, 1) == 0
        assert await count_running_submissions(conn, 4) == 1

        # Rewrites the fixture, so it goes last. The connection is never
        # committed: like the other mutations, this one leaves that to the
        # caller, and the assertions below read it back inside the transaction.
        assert await regrade_challenge(conn, 1) == 3

        statuses = (
            await conn.execute(
                select(Submission.c.status).where(Submission.c.challenge_id == 1)
            )
        ).scalars().all()
        assert set(statuses) == {cm.SubmissionStatus.queued}
        assert [tc.n_outcomes for tc in await query_challenge_testcases(conn, 1)] == [
            0, 0, 0,
        ]

        # "Sum" is on the same course and shares nothing else: a missing
        # challenge_id filter would have wiped it too.
        assert [tc.n_outcomes for tc in await query_challenge_testcases(conn, 2)] == [
            1, 1, 1,
        ]
        assert (
            await conn.execute(
                select(Submission.c.status).where(Submission.c.challenge_id == 2)
            )
        ).scalars().all() == [cm.SubmissionStatus.done]
    await engine.dispose()

