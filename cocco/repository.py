"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import JSON, Connection, cast, func, insert, select, update

from cocco.schema import (
    Challenge,
    SubmissionRunData,
    TestCase,
    TestOutcome,
    Submission,
)
from cocco.model import (
    SubmissionStatus,
    challenges,
    submissions,
    test_cases,
    test_outcomes,
)


async def get_queued_submission(conn: Connection) -> SubmissionRunData | None:
    """Get a queued submission, along with all the data required to run it."""
    # we should mark the submission as running here, using a single atomic
    # operation, otherwise two workers might dequeue the same submission!

    submission = conn.execute(
        select(submissions)
        .where(submissions.c.status == SubmissionStatus.queued)
        .order_by(submissions.c.enqueued)
    ).first()
    if submission is None:
        return None

    challenge = conn.execute(
        select(challenges)
        .where(challenges.c.id == submission.id)
    ).first()
    if challenge is None:
        return None

    tests = conn.execute(
        select(test_cases)
        .where(test_cases.c.challenge_id == challenge.id)
    ).all()

    return SubmissionRunData(
        submission_id=submission.id,
        max_threads=challenge.max_threads,
        max_memory_bytes=challenge.max_memory_bytes,
        max_time_s=challenge.max_time_s,
        test_cases=[TestCase.model_validate(t) for t in tests],
    )


def get_challenge(conn: Connection, challenge_id: int) -> Challenge | None:
    res = (
        conn.execute(select(challenges).where(challenges.c.id == challenge_id))
        .mappings()
        .first()
    )
    if res is None:
        return None
    return Challenge.model_validate(res)


def get_tests_for_challenge(conn: Connection, challenge_id: int) -> list[TestCase]:
    rows = (
        conn.execute(
            select(test_cases).where(test_cases.c.challenge_id == challenge_id)
        )
        .mappings()
        .all()
    )
    return [TestCase.model_validate(r) for r in rows]


def mark_submission_running(conn: Connection, submission_id: int) -> None:
    conn.execute(
        update(submissions)
        .where(submissions.c.id == submission_id)
        .values(status=SubmissionStatus.running)
    )


def mark_submission_done(conn: Connection, submission_id: int) -> None:
    conn.execute(
        update(submissions)
        .where(submissions.c.id == submission_id)
        .values(status=SubmissionStatus.done)
    )


def insert_test_outcomes(conn: Connection, outcomes: list[TestOutcome]) -> None:
    if not outcomes:
        return
    conn.execute(
        insert(test_outcomes),
        [o.model_dump(exclude={"id"}) for o in outcomes],
    )
