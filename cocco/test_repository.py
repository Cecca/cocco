from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, insert
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
    pull_queued_submission,
    query_challenge,
    query_challenge_testcases,
    query_user_role,
    query_user_submissions,
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
                        is_sample=order == 0,
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
        # one of the three test cases per challenge is public
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

        testcases = await query_challenge_testcases(conn, 1)
        assert [tc.execution_order for tc in testcases] == [0, 1, 2]
        assert [tc.is_sample for tc in testcases] == [True, False, False]
    await engine.dispose()

