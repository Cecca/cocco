from datetime import datetime, timedelta

from sqlalchemy import create_engine, insert
from icecream import ic

import cocco.model as cm
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
from cocco.repository import get_user_focused_challenge_info, pull_queued_submission
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
        user_ids = {
            nickname: conn.execute(
                insert(User)
                .values(email=email, nickname=nickname)
                .returning(User.c.id)
            ).scalar_one()
            for email, nickname in users
        }

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
        user_id = conn.execute(
            insert(User)
            .values(
                email="me@example.com",
                nickname = "the-admin"
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
            )
            .returning(Challenge.c.id)
        ).scalar_one()

        oldest_id = conn.execute(
            insert(Submission)
            .values(
                challenge_id=challenge_id,
                user_id=user_id,
                language="python",
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


def test_user_focused_challenge_info():
    import os
    url = f"postgresql+psycopg://{os.environ['PGUSER']}@/cocco-dev?host={os.environ['PGHOST']}"
    engine = _make_example_database(url)
    with engine.connect() as conn:
        res = get_user_focused_challenge_info(conn, 4, 1)
        ic(res)
    
