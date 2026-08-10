"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import Connection, and_, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

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


async def insert_submission(
    conn: AsyncConnection,
    user_id: int,
    challenge_id: int,
    language: str,
    filename: str,
    source: str,
) -> int:
    """Enqueue a new submission. Like `insert_enrollment`, this does not commit:
    the caller owns the transaction."""
    stmt = (
        insert(Submission)
        .values(
            user_id=user_id,
            challenge_id=challenge_id,
            language=language,
            filename=filename,
            source=source,
            status=SubmissionStatus.queued,
        )
        .returning(Submission.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


async def query_user_submissions(conn: AsyncConnection, user_id: int, challenge_id: int):
    """All the attempts of a user on a challenge, newest first.

    Submissions are LEFT JOINed to their outcomes so that `queued` and `running`
    ones - which have no outcomes yet - still show up: those are exactly the
    rows the challenge page polls for.

    The aggregate `score` is the mean of the per-test scores. Note that this is
    only meaningful for higher-is-better evaluators; `RMSEEvaluator` returns a
    score where lower is better, and this aggregation gets it backwards.
    """
    query = text("""
    WITH
    outcomes AS (
        SELECT submission_id,
               json_agg(testoutcome ORDER BY testoutcome.id) AS outcome,
               avg(coalesce(score, 0)) AS score,
               sum(elapsed_s) AS elapsed_s,
               count(*) FILTER (WHERE outcome = 'success') AS n_passed,
               count(*) AS n_tests
        FROM testoutcome
        GROUP BY submission_id
    ),
    attempts AS (
        SELECT submission.*,
               outcomes.outcome,
               outcomes.score,
               outcomes.elapsed_s,
               outcomes.n_passed,
               outcomes.n_tests,
               row_number() OVER (ORDER BY submission.enqueued) AS attempt
        FROM submission
        LEFT JOIN outcomes ON submission.id = outcomes.submission_id
        WHERE submission.challenge_id = :challenge_id
          AND submission.user_id = :user_id
    ),
    ranked AS (
        SELECT attempts.*,
               attempts.score IS NOT NULL
               AND row_number() OVER (
                   ORDER BY attempts.score DESC NULLS LAST, attempts.attempt DESC
               ) = 1 AS is_best
        FROM attempts
    )
    SELECT json_agg(ranked ORDER BY ranked.attempt DESC) FROM ranked;
    """)

    res = await conn.execute(query, dict(user_id=user_id, challenge_id=challenge_id))
    return res.scalar_one_or_none()


async def query_challenge(conn: AsyncConnection, challenge_id: int):
    """The challenge itself, its course, its position within the course and its
    public sample test cases.

    Submissions are deliberately not part of this document: the challenge page
    loads them through `query_user_submissions` and keeps them fresh by polling.
    """
    query = text("""
    WITH
    samples AS (
        SELECT challenge_id,
               json_agg(json_build_object('id', id, 'execution_order', execution_order)
                        ORDER BY execution_order) AS samples
        FROM testcase
        WHERE is_sample
        GROUP BY challenge_id
    ),
    positions AS (
        SELECT id,
               row_number() OVER (PARTITION BY course_id ORDER BY id) AS position
        FROM challenge
    ),
    cs AS (
        SELECT challenge.*,
               course.title AS course_title,
               positions.position AS position,
               coalesce(samples.samples, '[]'::json) AS samples
        FROM challenge
        LEFT JOIN course ON course.id = challenge.course_id
        LEFT JOIN positions ON positions.id = challenge.id
        LEFT JOIN samples ON samples.challenge_id = challenge.id
        WHERE challenge.id = :challenge_id
    )
    SELECT row_to_json(cs) FROM cs;
    """)

    res = await conn.execute(query, dict(challenge_id=challenge_id))
    return res.scalar_one_or_none()


async def query_challenge_leaderboard(
    conn: AsyncConnection, challenge_id: int, limit: int = 10
):
    """Ranking of the users who solved the challenge, fastest first.

    A submission qualifies only if it is done and every one of its tests
    succeeded; its time is the sum of the per-test running times. Each user is
    represented by their fastest qualifying submission.
    """
    query = text("""
    WITH
    per_submission AS (
        SELECT submission.id,
               submission.user_id,
               sum(testoutcome.elapsed_s) AS elapsed_s,
               bool_and(testoutcome.outcome = 'success') AS all_passed
        FROM submission
        JOIN testoutcome ON testoutcome.submission_id = submission.id
        WHERE submission.challenge_id = :challenge_id
          AND submission.status = 'done'
        GROUP BY submission.id, submission.user_id
    ),
    best AS (
        SELECT DISTINCT ON (user_id) user_id, id AS submission_id, elapsed_s
        FROM per_submission
        WHERE all_passed AND elapsed_s IS NOT NULL
        ORDER BY user_id, elapsed_s
    ),
    board AS (
        SELECT row_number() OVER (ORDER BY best.elapsed_s) AS rank,
               best.user_id,
               best.submission_id,
               best.elapsed_s,
               "user".nickname
        FROM best
        JOIN "user" ON "user".id = best.user_id
        ORDER BY best.elapsed_s
        LIMIT :limit
    )
    SELECT json_agg(board ORDER BY board.rank) FROM board;
    """)

    res = await conn.execute(
        query, dict(challenge_id=challenge_id, limit=limit)
    )
    return res.scalar_one_or_none()


async def query_sample_testcase(
    conn: AsyncConnection, challenge_id: int, testcase_id: int
):
    """The paths of a *public* test case, or None. Used to authorize downloads:
    hidden test cases must never be served."""
    stmt = select(TestCase.c.execution_order, TestCase.c.input_path,
                  TestCase.c.expected_output_path).where(
        and_(
            TestCase.c.id == testcase_id,
            TestCase.c.challenge_id == challenge_id,
            TestCase.c.is_sample,
        )
    )
    return (await conn.execute(stmt)).first()


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

