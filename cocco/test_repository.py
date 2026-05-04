from datetime import datetime, timedelta

from sqlalchemy import create_engine, insert

import cocco.model as cm
from cocco.model import challenges, metadata, submissions
from cocco.repository import get_queued_submission


def _make_engine():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


async def test_get_queued_submission_returns_oldest_queued():
    engine = _make_engine()
    now = datetime.now()

    with engine.begin() as conn:
        challenge_id = conn.execute(
            insert(challenges)
            .values(
                title="Hello",
                description="say hi",
                max_memory_bytes=1 << 20,
                max_time_s=1.0,
            )
            .returning(challenges.c.id)
        ).scalar_one()

        oldest_id = conn.execute(
            insert(submissions)
            .values(
                challenge_id=challenge_id,
                language="python",
                source="print('a')",
                status=cm.SubmissionStatus.queued,
                enqueued=now - timedelta(seconds=10),
            )
            .returning(submissions.c.id)
        ).scalar_one()

        conn.execute(
            insert(submissions).values(
                challenge_id=challenge_id,
                language="python",
                source="print('b')",
                status=cm.SubmissionStatus.queued,
                enqueued=now,
            )
        )
        conn.execute(
            insert(submissions).values(
                challenge_id=challenge_id,
                language="python",
                source="print('c')",
                status=cm.SubmissionStatus.running,
                enqueued=now - timedelta(seconds=20),
            )
        )

    with engine.connect() as conn:
        result = await get_queued_submission(conn)

    assert result is not None
    assert result.id == oldest_id
    assert result.status == cm.SubmissionStatus.queued


async def test_get_queued_submission_returns_none_when_empty():
    engine = _make_engine()
    with engine.connect() as conn:
        result = await get_queued_submission(conn)
    assert result is None
