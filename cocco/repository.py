"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import Connection, insert, select, update

from cocco.model import (
    Challenge,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    challenges,
    submissions,
    test_cases,
    test_outcomes,
)


async def get_queued_submission(conn: Connection) -> Submission | None:
    """Get a queued submission, along with all the data required to run it."""
    query = (
        select(submissions)
        .order_by(submissions.c.enqueued)
        .where(submissions.c.status == SubmissionStatus.queued)
    )
    res = conn.execute(query).mappings().fetchone()
    if res is None:
        return None
    return Submission.model_validate(res)


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
