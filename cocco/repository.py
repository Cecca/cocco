"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection
from icecream import ic

from cocco import schema
from cocco.schema import SubmissionRunData
from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    UserRole,
)


async def pull_queued_submission(conn: Connection) -> SubmissionRunData | None:
    """Get a queued submission, along with all the data required to run it.
    Atomically set it to 'running'."""
    # Get the submission to run, if any, and atomically sets it to running
    submission = conn.execute(
        update(Submission).where(
            Submission.c.id
            == (
                select(Submission.c.id)
                .where(Submission.c.status == SubmissionStatus.queued)
                .order_by(Submission.c.enqueued)
                .limit(1)
            ).scalar_subquery()
        ).values(status = SubmissionStatus.running)
        .returning(*Submission.c)
    ).first()
    if submission is None:
        return None
    conn.commit()

    challenge = conn.execute(
        select(Challenge).where(Challenge.c.id == submission.challenge_id)
    ).first()
    if challenge is None:
        return None

    tests = conn.execute(
        select(TestCase).where(TestCase.c.challenge_id == challenge.id)
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
        .where(Submission.c.id == submission_id)
        .values(status=SubmissionStatus.done)
    )


def insert_test_outcomes(conn: Connection, outcomes: list[schema.TestOutcome]) -> None:
    if not outcomes:
        return
    conn.execute(
        insert(TestOutcome),
        [o.model_dump(exclude={"id"}) for o in outcomes],
    )

async def insert_enrollment(conn: AsyncConnection, user_id: int, course_id: int, role: UserRole):
    stmt = insert(Enrollment).values(
        user_id=user_id,
        course_id=course_id,
        role=role
    )
    await conn.execute(stmt)


async def query_user_submissions(conn: AsyncConnection, user_id: int, challenge_id: int):
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
    )
    SELECT json_agg(submission)
    FROM submission
    LEFT JOIN outcomes ON submission.id = outcomes.submission_id
    WHERE challenge_id = :challenge_id
      AND user_id = :user_id;
    """)

    res = await conn.execute(query, dict(user_id=user_id, challenge_id=challenge_id))
    return res.scalar_one_or_none()

async def query_challenge(conn: AsyncConnection, user_id: int, challenge_id: int):
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
    cs AS ( SELECT challenge.*, subs.subs as submissions
        FROM challenge
        -- JOIN tc ON tc.challenge_id = challenge.id
        JOIN subs on subs.challenge_id = challenge.id)
    SELECT row_to_json(cs) FROM cs;
    """)

    res = await conn.execute(query, dict(user_id=user_id, challenge_id=challenge_id))
    return ic(res.scalar_one_or_none())


async def query_courses(conn: AsyncConnection, user_id: int):
    query = text("""
    WITH
    courses as (
        SELECT *
        FROM course c
        LEFT JOIN (SELECT * FROM enrollment WHERE user_id = :user_id) e
        ON c.id = e.course_id
    ),
    enrolled_courses AS (
        SELECT json_agg(courses) AS enrolled FROM courses WHERE role IS NOT null
    ),
    non_enrolled_courses AS (
        SELECT json_agg(courses) AS non_enrolled FROM courses WHERE role IS null
    ),
    u AS (
        SELECT * from enrolled_courses, non_enrolled_courses
    )
    SELECT row_to_json(u) FROM u;
    """)
    res = await conn.execute(query, dict(user_id=user_id))
    return res.scalar()


async def query_course_info(conn: AsyncConnection, course_id: int):
    query = text("""
    WITH
    course as (
        SELECT *
        FROM course c
        WHERE c.id = :course_id
    ),
    c AS (
        SELECT course.*, json_agg(challenge) as challenges
        FROM course
        JOIN challenge on course.id = challenge.course_id
        GROUP BY course.id, course.title, course.description
    )
    SELECT row_to_json(c) FROM c;
    """)
    res = await conn.execute(query, dict(course_id=course_id))
    return res.scalar()

