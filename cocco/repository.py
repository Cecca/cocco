"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import JSON, Connection, func, insert, select, update
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


def get_user_focused_challenge_info(conn: Connection, user_id: int, challenge_id: int) -> UserFocusedChallengeInfo | None:
    user_subq = (
        select(func.json_object("id", User.id, "email", User.email, "nickname", User.nickname))
        .where(User.id == user_id)
        .limit(1)
        .scalar_subquery()
    )
    # if user is None:
    #     return None
    # challenge = conn.execute(
    #     select(
    #         func.json_group_object("challenge", Challenge).where(
    #             Challenge.id == challenge_id
    #         )
    #     )
    # ).fetchone()
    # if challenge is None:
    #     return None

    pass
