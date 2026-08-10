"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from sqlalchemy import Connection, and_, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.schema import SubmissionRunData
from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    LeaderboardRanking,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
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

    # Retired test cases are skipped: they keep grading the submissions that
    # already ran against them, but nothing new is scored on them.
    tests = conn.execute(
        select(TestCase).where(
            and_(TestCase.c.challenge_id == challenge.id, TestCase.c.is_active)
        )
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


# The outcomes of one submission, joined to the test cases they were scored on.
# Shared by the two queries below, which differ only in how much of the
# submission itself they carry: `is_sample` is what tells the page whether the
# free-text fields may be shown to a student (see `visible_outcomes` in main.py).
_SUBMISSION_OUTCOMES_CTE = """
    outcomes AS (
        SELECT testoutcome.*,
               testcase.execution_order,
               testcase.is_sample,
               testcase.is_active
        FROM testoutcome
        JOIN testcase ON testcase.id = testoutcome.testcase_id
        WHERE testoutcome.submission_id = :submission_id
    ),
    -- Aggregated with no GROUP BY, so this is one row even when the submission
    -- has no outcomes yet and can be CROSS JOINed unconditionally below.
    agg AS (
        SELECT coalesce(json_agg(outcomes ORDER BY execution_order, id),
                        '[]'::json) AS outcomes,
               avg(coalesce(score, 0)) AS score,
               sum(elapsed_s) AS elapsed_s,
               count(*) FILTER (WHERE outcome = 'success') AS n_passed,
               count(*) AS n_tests
        FROM outcomes
    )
"""


async def query_submission(conn: AsyncConnection, submission_id: int):
    """One submission in full: its source, who wrote it, where it belongs, and
    every outcome it was scored on.

    This is the only query that returns `source`, which is why the fragment that
    polls the same page uses `query_submission_outcomes` instead.

    `attempt` is the submission's position in its author's history on the
    challenge, computed exactly as `query_user_submissions` does it, so the "#3"
    on this page is the "#3" of the table the viewer clicked.
    """
    query = text(f"""
    WITH
    {_SUBMISSION_OUTCOMES_CTE},
    attempts AS (
        SELECT id,
               row_number() OVER (ORDER BY enqueued) AS attempt
        FROM submission
        WHERE (user_id, challenge_id) = (
            SELECT user_id, challenge_id FROM submission WHERE id = :submission_id
        )
    ),
    s AS (
        SELECT submission.*,
               "user".nickname AS author_nickname,
               "user".email AS author_email,
               challenge.title AS challenge_title,
               challenge.course_id AS course_id,
               course.title AS course_title,
               attempts.attempt,
               agg.outcomes,
               agg.score,
               agg.elapsed_s,
               agg.n_passed,
               agg.n_tests
        FROM submission
        JOIN "user" ON "user".id = submission.user_id
        JOIN challenge ON challenge.id = submission.challenge_id
        LEFT JOIN course ON course.id = challenge.course_id
        LEFT JOIN attempts ON attempts.id = submission.id
        CROSS JOIN agg
        WHERE submission.id = :submission_id
    )
    SELECT row_to_json(s) FROM s;
    """)

    res = await conn.execute(query, dict(submission_id=submission_id))
    return res.scalar_one_or_none()


async def query_submission_outcomes(conn: AsyncConnection, submission_id: int):
    """What the submission page polls for: the status and the outcomes, and
    deliberately *not* the source, which never changes and can be a megabyte.

    `user_id` and `challenge_id` come along because the fragment route has to
    authorize the viewer just like the page does.
    """
    query = text(f"""
    WITH
    {_SUBMISSION_OUTCOMES_CTE},
    s AS (
        SELECT submission.id,
               submission.user_id,
               submission.challenge_id,
               submission.status,
               agg.outcomes,
               agg.score,
               agg.elapsed_s,
               agg.n_passed,
               agg.n_tests
        FROM submission
        CROSS JOIN agg
        WHERE submission.id = :submission_id
    )
    SELECT row_to_json(s) FROM s;
    """)

    res = await conn.execute(query, dict(submission_id=submission_id))
    return res.scalar_one_or_none()


async def query_challenge(conn: AsyncConnection, user_id: int, challenge_id: int):
    """The challenge itself, its course, its position within the course, the
    viewer's role in that course and its public sample test cases.

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
        WHERE is_sample AND is_active
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
               enrollment.role AS viewer_role,
               coalesce(samples.samples, '[]'::json) AS samples
        FROM challenge
        LEFT JOIN course ON course.id = challenge.course_id
        LEFT JOIN positions ON positions.id = challenge.id
        LEFT JOIN samples ON samples.challenge_id = challenge.id
        LEFT JOIN enrollment ON enrollment.course_id = challenge.course_id
                            AND enrollment.user_id = :user_id
        WHERE challenge.id = :challenge_id
    )
    SELECT row_to_json(cs) FROM cs;
    """)

    res = await conn.execute(query, dict(challenge_id=challenge_id, user_id=user_id))
    return res.scalar_one_or_none()


# What each ranking criterion means, as (qualifier, ordering) pairs of literal
# SQL spliced into the leaderboard query below. They are written here and keyed
# by the enum - no request data ever reaches the query as text.
#
# The speed board keeps its historical rule: only a submission that passed every
# test is on it. The score boards take any submission that was actually graded,
# because a partial-credit evaluator is exactly the case where nobody passes
# everything, and they fall back on time to break a tie.
_LEADERBOARD_CRITERIA: dict[LeaderboardRanking, tuple[str, str]] = {
    LeaderboardRanking.running_time: (
        "all_passed AND elapsed_s IS NOT NULL",
        "elapsed_s ASC",
    ),
    LeaderboardRanking.total_score: (
        "total_score IS NOT NULL",
        "total_score DESC, elapsed_s ASC NULLS LAST",
    ),
    LeaderboardRanking.average_score: (
        "average_score IS NOT NULL",
        "average_score DESC, elapsed_s ASC NULLS LAST",
    ),
}


async def query_challenge_ranking(
    conn: AsyncConnection, challenge_id: int
) -> LeaderboardRanking | None:
    """The challenge's leaderboard criterion, or None if there is no such
    challenge. Its own query because the leaderboard fragment polls every few
    seconds and has no use for the rest of `query_challenge`'s document."""
    res = await conn.execute(
        select(Challenge.c.ranking).where(Challenge.c.id == challenge_id)
    )
    return res.scalar_one_or_none()


async def query_challenge_leaderboard(
    conn: AsyncConnection,
    challenge_id: int,
    ranking: LeaderboardRanking = LeaderboardRanking.running_time,
    limit: int = 10,
):
    """Ranking of the users who did best on the challenge, best first.

    "Best" is whatever the challenge says: the total running time of a fully
    correct submission, the sum of its per-test scores, or their mean - see
    `_LEADERBOARD_CRITERIA`. Only submissions that are done and have outcomes
    count, and each user is represented by their single best one. Every metric
    comes back on every row, so a row carries the number the criterion ranked on
    plus the `n_tests` the scores are out of.
    """
    qualifier, order = _LEADERBOARD_CRITERIA[LeaderboardRanking(ranking)]
    query = text(f"""
    WITH
    per_submission AS (
        SELECT submission.id,
               submission.user_id,
               sum(testoutcome.elapsed_s) AS elapsed_s,
               sum(testoutcome.score) AS total_score,
               avg(testoutcome.score) AS average_score,
               count(testoutcome.id) AS n_tests,
               bool_and(testoutcome.outcome = 'success') AS all_passed
        FROM submission
        JOIN testoutcome ON testoutcome.submission_id = submission.id
        WHERE submission.challenge_id = :challenge_id
          AND submission.status = 'done'
        GROUP BY submission.id, submission.user_id
    ),
    best AS (
        SELECT DISTINCT ON (user_id)
               user_id,
               id AS submission_id,
               elapsed_s,
               total_score,
               average_score,
               n_tests
        FROM per_submission
        WHERE {qualifier}
        ORDER BY user_id, {order}, id
    ),
    board AS (
        SELECT row_number() OVER (ORDER BY {order}, submission_id) AS rank,
               best.user_id,
               best.submission_id,
               best.elapsed_s,
               best.total_score,
               best.average_score,
               best.n_tests,
               "user".nickname
        FROM best
        JOIN "user" ON "user".id = best.user_id
        ORDER BY rank
        LIMIT :limit
    )
    SELECT json_agg(board ORDER BY board.rank) FROM board;
    """)

    res = await conn.execute(
        query, dict(challenge_id=challenge_id, limit=limit)
    )
    return res.scalar_one_or_none()


async def query_challenge_standings(conn: AsyncConnection, challenge_id: int):
    """How the whole class is doing on a challenge - one row per student.

    This is the staff view of `query_user_submissions`: every student is
    represented by their best attempt, ranked by score and then by total running
    time. Students who are enrolled but have handed in nothing appear too, with
    a null score, which is exactly what a teacher wants to see.

    The cohort is the union of the enrolled students and everyone who has
    submitted: a submission can outlive an enrollment, and a course may be
    seeded with submissions before anyone is enrolled.

    As in `query_user_submissions`, `score` is the mean of the per-test scores
    and so only means anything for a higher-is-better evaluator.
    """
    query = text("""
    WITH
    cohort AS (
        SELECT user_id FROM submission WHERE challenge_id = :challenge_id
        UNION
        SELECT enrollment.user_id
        FROM enrollment
        JOIN challenge ON challenge.course_id = enrollment.course_id
        WHERE challenge.id = :challenge_id AND enrollment.role = 'student'
    ),
    outcomes AS (
        SELECT submission_id,
               avg(coalesce(score, 0)) AS score,
               sum(elapsed_s) AS elapsed_s,
               count(*) FILTER (WHERE outcome = 'success') AS n_passed,
               count(*) AS n_tests
        FROM testoutcome
        GROUP BY submission_id
    ),
    -- Left joined, exactly as in query_user_submissions: a queued or running
    -- attempt has no outcomes yet and must keep a *null* score rather than a
    -- zero, both so it is not ranked as a failure and so it can be counted as
    -- pending below.
    attempts AS (
        SELECT submission.id,
               submission.user_id,
               submission.status,
               submission.enqueued,
               outcomes.score,
               outcomes.elapsed_s,
               outcomes.n_passed,
               outcomes.n_tests
        FROM submission
        LEFT JOIN outcomes ON outcomes.submission_id = submission.id
        WHERE submission.challenge_id = :challenge_id
    ),
    best AS (
        SELECT DISTINCT ON (user_id)
               user_id, id AS best_submission_id, score, elapsed_s,
               n_passed, n_tests
        FROM attempts
        ORDER BY user_id, score DESC NULLS LAST, elapsed_s ASC NULLS LAST
    ),
    totals AS (
        SELECT user_id,
               count(*) AS n_attempts,
               count(*) FILTER (WHERE status IN ('queued', 'running')) AS n_pending,
               max(enqueued) AS last_enqueued
        FROM attempts
        GROUP BY user_id
    ),
    board AS (
        SELECT row_number() OVER (
                   ORDER BY best.score DESC NULLS LAST,
                            best.elapsed_s ASC NULLS LAST,
                            "user".nickname
               ) AS rank,
               cohort.user_id,
               "user".nickname,
               best.best_submission_id,
               best.score,
               best.elapsed_s,
               best.n_passed,
               best.n_tests,
               coalesce(totals.n_attempts, 0) AS n_attempts,
               coalesce(totals.n_pending, 0) AS n_pending,
               totals.last_enqueued
        FROM cohort
        JOIN "user" ON "user".id = cohort.user_id
        LEFT JOIN best ON best.user_id = cohort.user_id
        LEFT JOIN totals ON totals.user_id = cohort.user_id
    )
    SELECT json_agg(board ORDER BY board.rank) FROM board;
    """)

    res = await conn.execute(query, dict(challenge_id=challenge_id))
    return res.scalar_one_or_none()


async def count_running_submissions(conn: AsyncConnection, challenge_id: int) -> int:
    """How many submissions of a challenge a judge is working on right now.

    A regrade refuses while this is non-zero: the in-flight run would write its
    outcomes and mark the submission done *after* the reset, leaving one stale
    grade behind with nothing to say it is stale.
    """
    stmt = select(func.count()).where(
        and_(
            Submission.c.challenge_id == challenge_id,
            Submission.c.status == SubmissionStatus.running,
        )
    )
    return (await conn.execute(stmt)).scalar_one()


async def query_sample_testcase(
    conn: AsyncConnection, challenge_id: int, testcase_id: int
):
    """The paths of a *public* test case, or None. Used to authorize downloads:
    hidden test cases must never be served, and neither must retired ones - a
    sample nothing is graded against is not an example of anything."""
    stmt = select(TestCase.c.execution_order, TestCase.c.input_path,
                  TestCase.c.expected_output_path).where(
        and_(
            TestCase.c.id == testcase_id,
            TestCase.c.challenge_id == challenge_id,
            TestCase.c.is_sample,
            TestCase.c.is_active,
        )
    )
    return (await conn.execute(stmt)).first()


async def query_user_role(
    conn: AsyncConnection, user_id: int, challenge_id: int
) -> UserRole | None:
    """The viewer's role in the course a challenge belongs to, or None if they
    are not enrolled. This is the authorization check for the editor."""
    stmt = (
        select(Enrollment.c.role)
        .select_from(
            Challenge.join(
                Enrollment, Enrollment.c.course_id == Challenge.c.course_id
            )
        )
        .where(
            and_(Challenge.c.id == challenge_id, Enrollment.c.user_id == user_id)
        )
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


async def query_user(conn: AsyncConnection, user_id: int):
    """A user's public identity, without the avatar blob."""
    stmt = select(User.c.id, User.c.email, User.c.nickname).where(
        User.c.id == user_id
    )
    return (await conn.execute(stmt)).first()


async def query_challenge_testcases(conn: AsyncConnection, challenge_id: int):
    """Every test case of a challenge, in execution order, with the number of
    outcomes scored against each.

    Unlike the `samples` array of `query_challenge`, this includes the hidden
    and the retired ones - the editor lists them all, and unretiring one has to
    be possible. `n_outcomes` is what tells the editor which rows can still be
    deleted.
    """
    stmt = (
        select(TestCase, func.count(TestOutcome.c.id).label("n_outcomes"))
        .select_from(
            TestCase.outerjoin(
                TestOutcome, TestOutcome.c.testcase_id == TestCase.c.id
            )
        )
        .where(TestCase.c.challenge_id == challenge_id)
        .group_by(TestCase.c.id)
        .order_by(TestCase.c.execution_order, TestCase.c.id)
    )
    return (await conn.execute(stmt)).all()


async def count_testcase_outcomes(conn: AsyncConnection, testcase_id: int) -> int:
    """How many submissions were graded on a test case. Non-zero means it must
    not be deleted - see `delete_testcase`."""
    stmt = select(func.count()).where(TestOutcome.c.testcase_id == testcase_id)
    return (await conn.execute(stmt)).scalar_one()


async def query_testcase(conn: AsyncConnection, challenge_id: int, testcase_id: int):
    """A single test case, scoped to its challenge so that an id from another
    challenge cannot be manipulated through this challenge's routes."""
    stmt = select(TestCase).where(
        and_(TestCase.c.id == testcase_id, TestCase.c.challenge_id == challenge_id)
    )
    return (await conn.execute(stmt)).first()


# The following mutations deliberately do not commit: the caller owns the
# transaction, as with `insert_enrollment` and `insert_submission`.


async def regrade_challenge(conn: AsyncConnection, challenge_id: int) -> int:
    """Throw away every recorded outcome of a challenge and requeue its
    submissions. Returns how many were requeued.

    This is how a fixed expected output or a corrected time limit is applied
    retroactively, without asking students to submit again. Two consequences are
    deliberate:

    - Outcomes scored against *retired* test cases go with the rest and never
      come back, because `pull_queued_submission` only runs the active ones.
      Regrading is the destructive option; retiring a test case is the one that
      preserves history.
    - Afterwards no outcome points at any of the challenge's test cases, so
      `count_testcase_outcomes` is zero for all of them and they become
      deletable again. That is consistent rather than a hole in the rule: there
      are no scores left for a delete to strand.

    The caller must check `count_running_submissions` first, or a judge that is
    mid-run will write the outcomes of the old grading after the reset.
    """
    submission_ids = select(Submission.c.id).where(
        Submission.c.challenge_id == challenge_id
    )
    await conn.execute(
        delete(TestOutcome).where(TestOutcome.c.submission_id.in_(submission_ids))
    )
    result = await conn.execute(
        update(Submission)
        .where(Submission.c.challenge_id == challenge_id)
        .values(status=SubmissionStatus.queued)
    )
    return result.rowcount


async def update_challenge(conn: AsyncConnection, challenge_id: int, **fields) -> None:
    if not fields:
        return
    await conn.execute(
        update(Challenge).where(Challenge.c.id == challenge_id).values(**fields)
    )


async def insert_testcase(
    conn: AsyncConnection,
    challenge_id: int,
    input_path: str,
    expected_output_path: str,
    is_sample: bool = False,
) -> int:
    """Append a test case after the existing ones."""
    next_order = (
        await conn.execute(
            select(func.coalesce(func.max(TestCase.c.execution_order) + 1, 0)).where(
                TestCase.c.challenge_id == challenge_id
            )
        )
    ).scalar_one()
    stmt = (
        insert(TestCase)
        .values(
            challenge_id=challenge_id,
            execution_order=next_order,
            input_path=input_path,
            expected_output_path=expected_output_path,
            is_sample=is_sample,
        )
        .returning(TestCase.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


async def delete_testcase(conn: AsyncConnection, testcase_id: int) -> None:
    """Delete a test case nothing has been graded on.

    The caller must have checked `count_testcase_outcomes` first. A test case a
    submission was scored against is never deleted - detaching those outcomes
    would leave scores in the history with nothing to say what produced them, so
    staff retire the case (`is_active`) instead. The FK on
    `testoutcome.testcase_id` has no ON DELETE and stays the backstop: it raises
    rather than lets this quietly do the wrong thing.
    """
    await conn.execute(delete(TestCase).where(TestCase.c.id == testcase_id))


async def update_testcase(conn: AsyncConnection, testcase_id: int, **fields) -> None:
    if not fields:
        return
    await conn.execute(
        update(TestCase).where(TestCase.c.id == testcase_id).values(**fields)
    )


async def swap_testcase_order(conn: AsyncConnection, a_id: int, b_id: int) -> None:
    """Exchange the execution_order of two test cases.

    Done in three steps through a sentinel because `execution_order` may well
    gain a unique constraint per challenge later, and a direct swap would then
    collide mid-statement.
    """
    rows = (
        await conn.execute(
            select(TestCase.c.id, TestCase.c.execution_order).where(
                TestCase.c.id.in_([a_id, b_id])
            )
        )
    ).all()
    orders = {row.id: row.execution_order for row in rows}
    await conn.execute(
        update(TestCase).where(TestCase.c.id == a_id).values(execution_order=-1)
    )
    await conn.execute(
        update(TestCase)
        .where(TestCase.c.id == b_id)
        .values(execution_order=orders[a_id])
    )
    await conn.execute(
        update(TestCase)
        .where(TestCase.c.id == a_id)
        .values(execution_order=orders[b_id])
    )


async def query_courses(conn: AsyncConnection, user_id: int):
    query = text("""
    WITH
    courses as (
        SELECT *
        FROM course c
        LEFT JOIN (SELECT * FROM enrollment WHERE user_id = :user_id) e
        ON c.id = e.course_id
    ),
    -- coalesce: json_agg over no rows is NULL, and a user enrolled in every
    -- course (or none) would otherwise hand the template a null to iterate
    enrolled_courses AS (
        SELECT coalesce(json_agg(courses), '[]'::json) AS enrolled
        FROM courses WHERE role IS NOT null
    ),
    non_enrolled_courses AS (
        SELECT coalesce(json_agg(courses), '[]'::json) AS non_enrolled
        FROM courses WHERE role IS null
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

