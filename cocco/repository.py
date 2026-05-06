"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import JSON, Connection, func, insert, literal_column, select, text, update
from sqlalchemy.sql import functions
from sqlalchemy.ext.compiler import compiles

from cocco import schema
from cocco.schema import SubmissionRunData, UserFocusedChallengeInfo
from cocco.model import (
    Challenge,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
)


async def pull_queued_submission(conn: Connection) -> SubmissionRunData | None:
    """Get a queued submission, along with all the data required to run it.
    Atomically set it to 'running'."""
    # Get the submission to run, if any, and atomically sets it to running
    submission = conn.execute(
        update(Submission).where(
            Submission.id
            == (
                select(Submission.id)
                .where(Submission.status == SubmissionStatus.queued)
                .order_by(Submission.enqueued)
                .limit(1)
            ).scalar_subquery()
        ).values(status = SubmissionStatus.running)
        .returning(Submission)
    ).first()
    if submission is None:
        return None
    conn.commit()

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


def get_user_focused_challenge_info(conn: Connection, user_id: int, challenge_id: int):

    query = text("""
    WITH
    outcomes AS (
        SELECT submission_id, json_agg(testoutcome) as outcome  
        FROM testoutcome
        GROUP BY submission_id
    ),
    submission AS (
        SELECT submission.*, outcomes.outcome
        FROM submission
        JOIN outcomes ON submission.id = outcomes.submission_id
        ORDER BY enqueued
    ),
    subs AS (
        SELECT challenge_id, json_agg(submission) as subs
        FROM submission
        LEFT JOIN outcomes ON submission.id = outcomes.submission_id
        WHERE challenge_id = :challenge_id
          AND user_id = :user_id
        GROUP BY challenge_id
    ),
    cs AS ( SELECT challenge.*, subs.subs
        FROM challenge
        -- JOIN tc ON tc.challenge_id = challenge.id
        JOIN subs on subs.challenge_id = challenge.id)
    SELECT row_to_json(cs) FROM cs;
    """)

    # print(query)
    res = conn.execute(query, dict(user_id=user_id, challenge_id=challenge_id)).scalar()
    return res
