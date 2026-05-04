from datetime import datetime, timedelta

from sqlalchemy import create_engine, insert

import cocco.model as cm
from cocco.model import Base, Challenge, Course, Submission, User
from cocco.repository import get_queued_submission
from cocco.schema import SubmissionRunData


def _make_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


def _make_example_database():
    engine = _make_engine()

    with engine.begin() as conn:
        user_ids = [
            conn.execute(
                insert(User)
                .values(email=email, role=role, nickname=nickname)
                .returning(User.id)
            ).scalar_one()
            for email, role, nickname in [
                ("alice@example.com", cm.UserRole.teacher, "alice"),
                ("bob@example.com", cm.UserRole.student, "bob"),
                ("carol@example.com", cm.UserRole.student, "carol"),
            ]
        ]

        course_id = conn.execute(
            insert(Course)
            .values(title="Algorithms 101", description="intro course")
            .returning(Course.id)
        ).scalar_one()

        challenge_ids = [
            conn.execute(
                insert(Challenge)
                .values(
                    title=title,
                    description=description,
                    course_id=course_id,
                    max_memory_bytes=1 << 20,
                    max_time_s=1.0,
                )
                .returning(Challenge.id)
            ).scalar_one()
            for title, description in [
                ("Hello", "say hi"),
                ("Sum", "sum two numbers"),
            ]
        ]

        for i, user_id in enumerate(user_ids):
            conn.execute(
                insert(Submission).values(
                    user_id=user_id,
                    challenge_id=challenge_ids[i % len(challenge_ids)],
                    language="python",
                    source="print('hi')",
                    status=cm.SubmissionStatus.queued,
                )
            )

    return engine


async def test_get_queued_submission_returns_oldest_queued():
    engine = _make_engine()
    now = datetime.now()

    with engine.begin() as conn:
        challenge_id = conn.execute(
            insert(Challenge)
            .values(
                title="Hello",
                description="say hi",
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
            )
            .returning(Challenge.id)
        ).scalar_one()

        oldest_id = conn.execute(
            insert(Submission)
            .values(
                challenge_id=challenge_id,
                language="python",
                source="print('a')",
                status=cm.SubmissionStatus.queued,
                enqueued=now - timedelta(seconds=10),
            )
            .returning(Submission.id)
        ).scalar_one()

        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                language="python",
                source="print('b')",
                status=cm.SubmissionStatus.queued,
                enqueued=now,
            )
        )
        conn.execute(
            insert(Submission).values(
                challenge_id=challenge_id,
                language="python",
                source="print('c')",
                status=cm.SubmissionStatus.running,
                enqueued=now - timedelta(seconds=20),
            )
        )

    with engine.connect() as conn:
        result: SubmissionRunData | None = await get_queued_submission(conn)

    assert result is not None
    assert result.submission_id == oldest_id


# async def test_get_queued_submission_returns_none_when_empty():
#     engine = _make_engine()
#     with engine.connect() as conn:
#         result = await get_queued_submission(conn)
#     assert result is None
