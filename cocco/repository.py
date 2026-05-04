"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import Connection, insert, select, update

from cocco import schema
from cocco.schema import SubmissionRunData
from cocco.model import (
    Challenge,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
)


async def get_queued_submission(conn: Connection) -> SubmissionRunData | None:
    """Get a queued submission, along with all the data required to run it."""
    # we should mark the submission as running here, using a single atomic
    # operation, otherwise two workers might dequeue the same submission!

    submission = conn.execute(
        select(Submission)
        .where(Submission.status == SubmissionStatus.queued)
        .order_by(Submission.enqueued)
    ).first()
    if submission is None:
        return None

    challenge = conn.execute(
        select(Challenge).where(Challenge.id == submission.challenge_id)
    ).first()
    if challenge is None:
        return None

    tests = conn.execute(
        select(TestCase).where(TestCase.challenge_id == challenge.id)
    ).all()

    return SubmissionRunData(
        submission_id=submission.id,
        source=submission.source,
        language=submission.language,
        max_threads=challenge.max_threads,
        max_memory_bytes=challenge.max_memory_bytes,
        max_time_s=challenge.max_time_s,
        test_cases=[schema.TestCase.model_validate(t) for t in tests],
    )


def get_challenge(conn: Connection, challenge_id: int) -> schema.Challenge | None:
    res = conn.execute(
        select(Challenge).where(Challenge.id == challenge_id)
    ).mappings().first()
    if res is None:
        return None
    return schema.Challenge.model_validate(res)


def get_tests_for_challenge(conn: Connection, challenge_id: int) -> list[schema.TestCase]:
    rows = conn.execute(
        select(TestCase).where(TestCase.challenge_id == challenge_id)
    ).mappings().all()
    return [schema.TestCase.model_validate(r) for r in rows]


def mark_submission_running(conn: Connection, submission_id: int) -> None:
    conn.execute(
        update(Submission)
        .where(Submission.id == submission_id)
        .values(status=SubmissionStatus.running)
    )


def mark_submission_done(conn: Connection, submission_id: int) -> None:
    conn.execute(
        update(Submission)
        .where(Submission.id == submission_id)
        .values(status=SubmissionStatus.done)
    )


def insert_test_outcomes(conn: Connection, outcomes: list[schema.TestOutcome]) -> None:
    if not outcomes:
        return
    conn.execute(
        insert(TestOutcome),
        [o.model_dump(exclude={"id"}) for o in outcomes],
    )
