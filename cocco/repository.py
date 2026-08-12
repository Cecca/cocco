"""\
This is the home to all the queries to the database.
Functions in this module return models from `cocco.models`,
or their combinations
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import (
    Connection,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.schema import SubmissionRunData
from cocco.model import (
    Administrator,
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
        evaluator_path=challenge.evaluator_path,
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

async def insert_user(
    conn: AsyncConnection, email: str, nickname: str, image: bytes, image_mime: str
) -> int:
    """Create a user and return their id. Does not commit.

    Raises `IntegrityError` on an address that already exists - `user.email`
    is unique, because it is what Google proves.
    """
    stmt = (
        insert(User)
        .values(
            email=email, nickname=nickname, image=image, image_mime=image_mime
        )
        .returning(User.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


async def insert_enrollment(conn: AsyncConnection, user_id: int, course_id: int, role: UserRole):
    stmt = insert(Enrollment).values(
        user_id=user_id,
        course_id=course_id,
        role=role
    )
    await conn.execute(stmt)


async def insert_self_enrollment(conn: AsyncConnection, user_id: int, course_id: int):
    """Enroll `user_id` on `course_id` as a student, unless they are on it already.

    The role is fixed here rather than taken from the caller: this is the one
    path into `enrollment` that no course staff approved, so it must not be able
    to name a role. `ON CONFLICT DO NOTHING` on the (user_id, course_id) primary
    key makes a double click - or two of them racing - a no-op instead of an
    IntegrityError, and is what guarantees an existing role is never
    overwritten: a teacher who reaches this route is not demoted to student.

    Like `insert_enrollment`, this does not commit: the caller owns the
    transaction.
    """
    stmt = (
        pg_insert(Enrollment)
        .values(user_id=user_id, course_id=course_id, role=UserRole.student)
        .on_conflict_do_nothing(index_elements=["user_id", "course_id"])
    )
    await conn.execute(stmt)


def _teacher_count(course_id: int):
    """How many teachers a course has, as a subquery.

    A subquery and not a separate `SELECT` because it is only ever asked as part
    of the statement that would change the answer - see `set_enrollment_role`
    and `delete_enrollment`, which both refuse to take the last one away.
    """
    return (
        select(func.count())
        .select_from(Enrollment)
        .where(
            Enrollment.c.course_id == course_id,
            Enrollment.c.role == UserRole.teacher,
        )
        .scalar_subquery()
    )


async def lock_course(conn: AsyncConnection, course_id: int) -> bool:
    """Take the course's row for update, so its membership changes serialize.
    False if there is no such course. Does not commit.

    Every mutation on the people page calls this first, and that is what makes
    "a course never loses its last teacher" true rather than merely usually
    true: `_teacher_count` runs inside the statement that acts on it, but under
    READ COMMITTED two concurrent statements would each still see the other's
    teacher and both succeed. Locking one row per course serializes them, and
    `get_db` hands out a connection whose transaction ends at the route's
    `commit`, so the lock is held for exactly the right span.
    """
    stmt = select(Course.c.id).where(Course.c.id == course_id).with_for_update()
    return (await conn.execute(stmt)).first() is not None


async def set_enrollment_role(
    conn: AsyncConnection, course_id: int, user_id: int, role: UserRole
) -> bool:
    """Put someone on a course in a given role, or change the role they are
    already in. Does not commit. True if a row was written.

    One upsert behind both halves of the people page - the enroll form and the
    per-row selector - because they are the same write: `enrollment` is keyed on
    (user_id, course_id), so "enroll them as a teacher" and "make them a
    teacher" name one row either way. Enrolling somebody twice is therefore not
    an `IntegrityError`, which is what `insert_enrollment` would raise.

    False means the update was refused: it would have taken `teacher` away from
    the last one a course has. That guard rides in the `ON CONFLICT` clause's
    WHERE rather than in a check preceding it, for the reason `block_user`
    gives - the rule and the write are then one statement. Promoting *to*
    teacher can never trip it, so it is not even attached in that case.
    """
    stmt = pg_insert(Enrollment).values(
        user_id=user_id, course_id=course_id, role=role
    )
    where = None
    if role is not UserRole.teacher:
        where = or_(
            Enrollment.c.role != UserRole.teacher, _teacher_count(course_id) > 1
        )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "course_id"],
        set_={"role": role},
        where=where,
    ).returning(Enrollment.c.user_id)
    return (await conn.execute(stmt)).first() is not None


async def delete_enrollment(
    conn: AsyncConnection, course_id: int, user_id: int
) -> bool:
    """Take somebody off a course. Does not commit. True if it did.

    Nothing of theirs goes with it. `submission.user_id` has no tie to
    `enrollment`, and `query_challenge_standings` builds its cohort as the union
    of the enrolled and everyone who submitted, so their attempts stay in the
    record of the course - a person is removed, their work is not retracted, the
    same distinction a block draws.

    False means the row belonged to the course's last teacher or was not there
    at all. The route reports both the same way, because in both cases nothing
    happened. Refusing on the last teacher lives in the WHERE clause for
    `block_user`'s reason, and is only sound under `lock_course`.
    """
    stmt = (
        delete(Enrollment)
        .where(
            Enrollment.c.course_id == course_id,
            Enrollment.c.user_id == user_id,
            or_(
                Enrollment.c.role != UserRole.teacher,
                _teacher_count(course_id) > 1,
            ),
        )
        .returning(Enrollment.c.user_id)
    )
    return (await conn.execute(stmt)).first() is not None


async def course_exists(conn: AsyncConnection, course_id: int) -> bool:
    """Whether the course is there at all. A POST naming a course that is not is
    a 404, rather than the foreign key violation - and 500 - an insert of an
    enrollment pointing at it would raise."""
    stmt = select(Course.c.id).where(Course.c.id == course_id)
    return (await conn.execute(stmt)).first() is not None


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
    only meaningful for higher-is-better evaluators: nothing stops a challenge's
    evaluator from scoring on another scale - an error metric, say - and this
    aggregation then gets it backwards.
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
    """The challenge itself, its course, its number within the course, the
    viewer's role in that course and its public sample test cases.

    `number` is not `challenge.position`: the column is the sort key the course
    editor's arrows write, and a swap leaves gaps in it. What the page prints
    is the rank of that key within the course, which is why the two are
    different fields here.

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
    numbers AS (
        SELECT id,
               row_number() OVER (PARTITION BY course_id ORDER BY position, id)
                   AS number
        FROM challenge
    ),
    cs AS (
        SELECT challenge.*,
               course.title AS course_title,
               numbers.number AS number,
               enrollment.role AS viewer_role,
               coalesce(samples.samples, '[]'::json) AS samples
        FROM challenge
        LEFT JOIN course ON course.id = challenge.course_id
        LEFT JOIN numbers ON numbers.id = challenge.id
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
    are not enrolled.

    For a caller that already knows the course - every route under
    `/courses/{course_id}/...` does - `query_course_role` is the one to use:
    it answers the same question without a challenge, which is what makes it
    usable before one exists.
    """
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


async def query_course_role(
    conn: AsyncConnection, user_id: int, course_id: int
) -> UserRole | None:
    """The viewer's role on a course, or None if they are not enrolled.

    This is the authorization check behind the editor: `role` lives on
    `enrollment`, so it is a straight lookup, and it holds for a course that
    has no challenges yet - which is exactly when a challenge is created.
    """
    stmt = select(Enrollment.c.role).where(
        and_(Enrollment.c.course_id == course_id, Enrollment.c.user_id == user_id)
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


async def is_administrator(conn: AsyncConnection, email: str) -> bool:
    """Whether this address administers the site.

    Keyed on the address rather than on a user id because that is what
    `administrator` holds - see the table, which has to be able to name someone
    who has never signed in. `user.email` is stored in the same normalized form
    the rows are, so this is a plain equality and needs no join.
    """
    stmt = select(Administrator.c.email).where(Administrator.c.email == email)
    return (await conn.execute(stmt)).first() is not None


async def insert_administrators(conn: AsyncConnection, emails: Iterable[str]) -> None:
    """Add administrators, leaving the ones already there alone. Does not commit.

    `ON CONFLICT DO NOTHING` is what makes the configured list a *bootstrap*:
    it is replayed on every boot, so it has to be idempotent, and it must never
    trample a row that came from somewhere else. Nothing here removes anybody -
    dropping a line from `cocco.toml` does not demote its owner.
    """
    rows = [{"email": email} for email in emails]
    if not rows:
        return
    await conn.execute(
        pg_insert(Administrator).on_conflict_do_nothing(index_elements=["email"]),
        rows,
    )


async def challenge_in_course(
    conn: AsyncConnection, challenge_id: int, course_id: int
) -> bool:
    """Whether a challenge really belongs to the course naming it in the URL.

    The editor takes the viewer's role from the `course_id` in the path, so
    that id has to be checked against the challenge rather than trusted:
    otherwise staff of one course could put their own course id in front of
    another course's challenge and edit it.
    """
    stmt = select(Challenge.c.id).where(
        and_(Challenge.c.id == challenge_id, Challenge.c.course_id == course_id)
    )
    return (await conn.execute(stmt)).first() is not None


"""The columns behind a `schema.User`.

Spelled once, because the block is asked about on every request and both
lookups below feed the same model: a list that carried `blocked_at` in one of
them and not the other would let a blocked person in through whichever door
forgot. `image` is deliberately absent - the avatar has a route of its own.
"""
_USER_COLUMNS = (
    User.c.id,
    User.c.email,
    User.c.nickname,
    User.c.blocked_at,
    User.c.blocked_reason,
)


async def query_user_by_email(conn: AsyncConnection, email: str):
    """The sibling of `query_user`, keyed on the address Google proved."""
    stmt = select(*_USER_COLUMNS).where(User.c.email == email)
    return (await conn.execute(stmt)).first()


async def query_user(conn: AsyncConnection, user_id: int):
    """A user's public identity, without the avatar blob."""
    stmt = select(*_USER_COLUMNS).where(User.c.id == user_id)
    return (await conn.execute(stmt)).first()


async def update_user(conn: AsyncConnection, user_id: int, **fields) -> None:
    """Change a user's own details. Does not commit.

    The kwargs shape `update_course` and `update_challenge` use, and for the
    same reason: the caller knows which half of the form it is saving. It is
    deliberately *not* how a block is written - `block_user` has a WHERE clause
    carrying a rule, and going through here would lose it. Nothing that touches
    `email` should come through here either: that is the login identifier
    Google proves, not a field.
    """
    if not fields:
        return
    await conn.execute(update(User).where(User.c.id == user_id).values(**fields))


async def block_user(
    conn: AsyncConnection, user_id: int, reason: str | None
) -> bool:
    """Shut an account out of the site. Does not commit. True if it did.

    That an administrator is never blocked lives in the WHERE clause rather than
    in a check preceding it, for two reasons. There is no way to revoke an
    administrator through the app, so a blocked one would be a lockout with no
    way back; and a check and a write that are two statements can be told apart
    by an appointment landing between them.

    False means the row belonged to an administrator or was not there at all.
    The route reports both the same way, because both mean nothing happened.
    """
    stmt = (
        update(User)
        .where(
            User.c.id == user_id,
            ~exists().where(Administrator.c.email == User.c.email),
        )
        .values(blocked_at=datetime.now(), blocked_reason=reason)
        .returning(User.c.id)
    )
    return (await conn.execute(stmt)).first() is not None


async def unblock_user(conn: AsyncConnection, user_id: int) -> bool:
    """Let an account back in. Does not commit. True if it did.

    Clears the reason with the block: it exists to be shown to the person it
    shut out, so it has no life after they are let back in.
    """
    stmt = (
        update(User)
        .where(User.c.id == user_id)
        .values(blocked_at=None, blocked_reason=None)
        .returning(User.c.id)
    )
    return (await conn.execute(stmt)).first() is not None


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


async def query_evaluator_path(
    conn: AsyncConnection, challenge_id: int
) -> str | None:
    """The challenge's evaluator script, or None for the bundled default.

    Its own query rather than a read of `query_challenge`: that one needs a
    viewer to compute their role, joins five tables, and the editor asks this
    on every mutation of the evaluator card.
    """
    result = await conn.execute(
        select(Challenge.c.evaluator_path).where(Challenge.c.id == challenge_id)
    )
    return result.scalar_one_or_none()


async def insert_challenge(
    conn: AsyncConnection,
    course_id: int,
    title: str,
    description: str,
    max_time_s: float,
    max_memory_bytes: int,
    max_threads: int,
    ranking: LeaderboardRanking,
) -> int:
    """Create a challenge on a course and return its id.

    It lands after the ones already there, exactly as `insert_testcase` appends
    within a challenge: a new challenge is the next one of the course, and the
    arrows in the course editor are for moving it somewhere else.

    `evaluator_path` is left NULL on purpose: a brand new challenge has chosen
    no checker, and the judge grades those with the bundled `equality.py`.
    Like the other insert helpers here, this does not commit - the caller owns
    the transaction.
    """
    next_position = (
        await conn.execute(
            select(func.coalesce(func.max(Challenge.c.position) + 1, 0)).where(
                Challenge.c.course_id == course_id
            )
        )
    ).scalar_one()
    stmt = (
        insert(Challenge)
        .values(
            course_id=course_id,
            title=title,
            description=description,
            max_time_s=max_time_s,
            max_memory_bytes=max_memory_bytes,
            max_threads=max_threads,
            ranking=ranking,
            position=next_position,
            evaluator_path=None,
        )
        .returning(Challenge.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


async def update_challenge(conn: AsyncConnection, challenge_id: int, **fields) -> None:
    if not fields:
        return
    await conn.execute(
        update(Challenge).where(Challenge.c.id == challenge_id).values(**fields)
    )


async def insert_course(conn: AsyncConnection, title: str, description: str) -> int:
    """Create a course and return its id.

    It carries no enrollment: a course with no staff is not useful, but who
    belongs on it is the caller's decision - the create route enrolls whoever
    made it. Like the other insert helpers here, this does not commit.
    """
    stmt = (
        insert(Course)
        .values(title=title, description=description)
        .returning(Course.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


async def update_course(conn: AsyncConnection, course_id: int, **fields) -> None:
    if not fields:
        return
    await conn.execute(
        update(Course).where(Course.c.id == course_id).values(**fields)
    )


async def query_ordered_challenge_ids(
    conn: AsyncConnection, course_id: int
) -> list[int]:
    """The ids of a course's challenges in the order the course page lists
    them - which is all a move needs to find the neighbour to swap with.

    Deliberately not `query_course_challenges`: the neighbour is two ids, and
    this way the ordering rule is written once, in plain Core, rather than a
    second time inside the page query's SQL.
    """
    stmt = (
        select(Challenge.c.id)
        .where(Challenge.c.course_id == course_id)
        .order_by(Challenge.c.position, Challenge.c.id)
    )
    return list((await conn.execute(stmt)).scalars())


async def reorder_challenges(conn: AsyncConnection, ordered_ids: list[int]) -> None:
    """Renumber a course's challenges so that `position` is their index in
    `ordered_ids`. The caller supplies the whole course, in the order it should
    end up in.

    A dense rewrite rather than the sentinel swap `swap_testcase_order` does,
    because `position` defaults to 0: every challenge of a course that predates
    the column - and every one the seed creates without it - starts out tied
    with all the others, and exchanging two zeroes moves nothing. Renumbering
    the whole course breaks every tie the first time an arrow is pressed, and
    keeps the numbers dense afterwards.

    Written in two passes through negative sentinels for the reason
    `swap_testcase_order` uses one: should `position` ever gain a unique
    constraint per course, assigning the final numbers one at a time would
    collide with the rows that have not been rewritten yet. `insert_challenge`
    only ever writes positions from 0 up, so the negatives are free.
    """
    for index, challenge_id in enumerate(ordered_ids):
        await conn.execute(
            update(Challenge)
            .where(Challenge.c.id == challenge_id)
            .values(position=-(index + 1))
        )
    for index, challenge_id in enumerate(ordered_ids):
        await conn.execute(
            update(Challenge)
            .where(Challenge.c.id == challenge_id)
            .values(position=index)
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


async def query_courses(conn: AsyncConnection, user_id: int, q: str = ""):
    """Every course, split into the ones this user is enrolled on and the rest.

    `q` filters both halves by title. It is the home page's filter box, so it
    is a substring test rather than a pattern: `position()` and not
    `ILIKE '%'||:q||'%'`, so a title typed with a `%` or a `_` in it is matched
    literally instead of turning into a wildcard, and there is no escaping to
    get wrong. An empty `q` is no filter at all.

    The columns are listed rather than `SELECT *`: `course.enrollment_key` is a
    secret, and a star would put it in the JSON of every card - including the
    courses the viewer is not on, which are precisely the ones it guards. What
    the card needs is `requires_key`, which is the question and not the answer.
    """
    query = text("""
    WITH
    courses as (
        SELECT
            c.id,
            c.title,
            c.description,
            c.enrollment_key IS NOT NULL AS requires_key,
            e.role
        FROM course c
        LEFT JOIN (SELECT * FROM enrollment WHERE user_id = :user_id) e
        ON c.id = e.course_id
        WHERE :q = '' OR position(lower(:q) in lower(c.title)) > 0
    ),
    -- coalesce: json_agg over no rows is NULL, and a user enrolled in every
    -- course (or none) would otherwise hand the template a null to iterate
    enrolled_courses AS (
        SELECT coalesce(json_agg(courses ORDER BY courses.title), '[]'::json) AS enrolled
        FROM courses WHERE role IS NOT null
    ),
    non_enrolled_courses AS (
        SELECT coalesce(json_agg(courses ORDER BY courses.title), '[]'::json) AS non_enrolled
        FROM courses WHERE role IS null
    ),
    u AS (
        SELECT * from enrolled_courses, non_enrolled_courses
    )
    SELECT row_to_json(u) FROM u;
    """)
    res = await conn.execute(query, dict(user_id=user_id, q=q))
    return res.scalar()


async def query_users(conn: AsyncConnection, q: str = ""):
    """Everyone the site knows about, for the admin page.

    Two lists, because `administrator` is keyed on an address and `user` on an
    account, and those are not the same set. `accounts` is everyone who has
    signed in at least once; `pending_administrators` is the addresses that have
    been appointed and have no account yet. Without the second, an
    administrator appointed before their first sign-in appears nowhere on this
    page - including the one just appointed by the form at the top of it, which
    would look like the click did nothing.

    `q` is a substring test on the address or the nickname, with `position()`
    rather than `ILIKE '%'||:q||'%'` for the reason `query_courses` gives: an
    address typed with a `%` or a `_` in it is matched literally and there is no
    escaping to get wrong. An empty `q` is no filter at all.

    The two counts are aggregated over the whole table and joined in, rather
    than asked per row: this page is the whole site and is opened rarely, so it
    is one round trip either way and this way it is one query.
    """
    query = text("""
    WITH
    enrolled AS (
        SELECT user_id, count(*) AS n_courses FROM enrollment GROUP BY user_id
    ),
    submitted AS (
        SELECT user_id,
               count(*) AS n_submissions,
               max(enqueued) AS last_submitted
        FROM submission GROUP BY user_id
    ),
    accounts AS (
        SELECT
            u.id,
            u.email,
            u.nickname,
            -- EXISTS rather than a join: `administrator` is keyed on the
            -- address and may name somebody with no account at all, so it is
            -- not a table every row of `user` is guaranteed to meet. That same
            -- fact is what `appointed` below exists for.
            EXISTS (
                SELECT 1 FROM administrator a WHERE a.email = u.email
            ) AS is_admin,
            u.blocked_at IS NOT NULL AS is_blocked,
            u.blocked_at,
            u.blocked_reason,
            coalesce(enrolled.n_courses, 0) AS n_courses,
            coalesce(submitted.n_submissions, 0) AS n_submissions,
            submitted.last_submitted
        FROM "user" u
        LEFT JOIN enrolled ON enrolled.user_id = u.id
        LEFT JOIN submitted ON submitted.user_id = u.id
        WHERE :q = ''
           OR position(lower(:q) in lower(u.email)) > 0
           OR position(lower(:q) in lower(u.nickname)) > 0
    ),
    -- coalesce, as in `query_courses`: json_agg over no rows is NULL, and a
    -- filter that matches nobody would otherwise hand the template a null.
    listed AS (
        SELECT coalesce(
            json_agg(accounts ORDER BY accounts.nickname, accounts.id),
            '[]'::json
        ) AS accounts
        FROM accounts
    ),
    appointed AS (
        SELECT administrator.email
        FROM administrator
        LEFT JOIN "user" ON "user".email = administrator.email
        WHERE "user".id IS NULL
          AND (:q = '' OR position(lower(:q) in lower(administrator.email)) > 0)
    ),
    pending AS (
        SELECT coalesce(json_agg(appointed ORDER BY appointed.email), '[]'::json)
               AS pending_administrators
        FROM appointed
    ),
    d AS (
        SELECT * FROM listed, pending
    )
    SELECT row_to_json(d) FROM d;
    """)
    res = await conn.execute(query, dict(q=q))
    return res.scalar()


async def query_user_profile(conn: AsyncConnection, user_id: int):
    """Everything one profile page shows, or None if there is no such account.

    `query_users` narrowed to one person and widened with the courses they are
    on - which is the thing a profile says that a row in a list cannot. The
    courses carry the role and the submissions made *there*, the way
    `query_course_people` counts per course rather than site-wide: on a profile
    the interesting number is per course, and the site-wide total rides along
    beside it as `n_submissions`.

    `person` is keyed on the id and nothing else, so a missing account makes
    every cross join below empty and `row_to_json` returns nothing at all -
    the route turns that into a 404 rather than rendering a blank page.

    `image` stays out, as it does in `_USER_COLUMNS`: the avatar has a route of
    its own and does not belong in a JSON document.
    """
    query = text("""
    WITH
    -- Site-wide, and beside the per-course counts rather than their sum: a
    -- submission to a course the person has since been removed from is still
    -- theirs, and would go missing from a total added up over `enrollment`.
    totals AS (
        SELECT count(*) AS n_submissions, max(enqueued) AS last_submitted
        FROM submission WHERE user_id = :user_id
    ),
    per_course AS (
        SELECT challenge.course_id, count(*) AS n_submissions
        FROM submission
        JOIN challenge ON challenge.id = submission.challenge_id
        WHERE submission.user_id = :user_id
        GROUP BY challenge.course_id
    ),
    memberships AS (
        SELECT
            c.id,
            c.title,
            e.role,
            coalesce(per_course.n_submissions, 0) AS n_submissions
        FROM enrollment e
        JOIN course c ON c.id = e.course_id
        LEFT JOIN per_course ON per_course.course_id = c.id
        WHERE e.user_id = :user_id
    ),
    -- coalesce, as everywhere else: json_agg over no rows is NULL, and
    -- somebody enrolled on nothing at all would hand the template a null to
    -- iterate. Staff first, then by title, as on the people page.
    listed AS (
        SELECT coalesce(
            json_agg(memberships ORDER BY memberships.role, memberships.title),
            '[]'::json
        ) AS courses
        FROM memberships
    ),
    person AS (
        SELECT
            u.id,
            u.email,
            u.nickname,
            -- EXISTS rather than a join, for `query_users`' reason:
            -- `administrator` is keyed on the address and need not have a row
            -- of `user` to meet.
            EXISTS (
                SELECT 1 FROM administrator a WHERE a.email = u.email
            ) AS is_admin,
            u.blocked_at IS NOT NULL AS is_blocked,
            u.blocked_at,
            u.blocked_reason
        FROM "user" u
        WHERE u.id = :user_id
    ),
    d AS (
        SELECT * FROM person, listed, totals
    )
    SELECT row_to_json(d) FROM d;
    """)
    res = await conn.execute(query, dict(user_id=user_id))
    return res.scalar()


async def query_course_people(conn: AsyncConnection, course_id: int, q: str = ""):
    """Everyone enrolled on a course, for the people page. Course staff only.

    `query_users` for one course instead of the whole site, and the differences
    are what a roster is: the row carries the person's `role` here, and the
    submission counts are over *this course* rather than over everything they
    have ever submitted - a teacher looking at their class wants to know who has
    started, not who is busy elsewhere.

    Strictly the enrolled, unlike `query_challenge_standings`, whose cohort is
    deliberately a union with everyone who submitted: this is the class list,
    and somebody who was removed is off it even though their attempts remain.
    A site administrator is staff on every course without an enrollment, so they
    do not appear here either - `effective_course_role` is where that is true,
    not this table.

    `n_teachers` rides along so the fragment can hide `Remove` on the last one.
    It is counted **outside** the `q` predicate on purpose: a filter that hides
    the other teachers must not make the visible one look like the last, which
    would put a refusal behind a button that is offered.

    `q` is the substring test the rest of the app uses - `position()` rather
    than `ILIKE '%'||:q||'%'`, so an address typed with a `%` or a `_` in it is
    matched literally and there is no escaping to get wrong.
    """
    query = text("""
    WITH
    submitted AS (
        SELECT submission.user_id,
               count(*) AS n_submissions,
               max(submission.enqueued) AS last_submitted
        FROM submission
        JOIN challenge ON challenge.id = submission.challenge_id
        WHERE challenge.course_id = :course_id
        GROUP BY submission.user_id
    ),
    members AS (
        SELECT
            u.id,
            u.email,
            u.nickname,
            e.role,
            u.blocked_at IS NOT NULL AS is_blocked,
            coalesce(submitted.n_submissions, 0) AS n_submissions,
            submitted.last_submitted
        FROM enrollment e
        JOIN "user" u ON u.id = e.user_id
        LEFT JOIN submitted ON submitted.user_id = u.id
        WHERE e.course_id = :course_id
          AND (:q = ''
               OR position(lower(:q) in lower(u.email)) > 0
               OR position(lower(:q) in lower(u.nickname)) > 0)
    ),
    -- coalesce, as in `query_users`: json_agg over no rows is NULL, and a
    -- filter that matches nobody would otherwise hand the template a null.
    -- Staff first, then by name: the people who run the course sit at the top.
    listed AS (
        SELECT coalesce(
            json_agg(members ORDER BY members.role, members.nickname, members.id),
            '[]'::json
        ) AS people
        FROM members
    ),
    -- Unfiltered, and the string literal is how `SAEnum(UserRole)` stores it -
    -- the spelling `query_challenge_standings` uses for `'student'`.
    staff AS (
        SELECT count(*) AS n_teachers
        FROM enrollment
        WHERE course_id = :course_id AND role = 'teacher'
    ),
    d AS (
        SELECT * FROM listed, staff
    )
    SELECT row_to_json(d) FROM d;
    """)
    res = await conn.execute(query, dict(course_id=course_id, q=q))
    return res.scalar()


async def query_course(conn: AsyncConnection, course_id: int):
    """A course and nothing else - no challenges, no aggregate.

    `query_course_info` is the page query; this is for the pages that only
    need the title, and being plain Core it runs anywhere.

    It also carries the description, which is what `get_course` renders for a
    visitor who is not enrolled: they see the course, not its contents, so the
    challenge list is never even fetched for them.

    `requires_key` and not the key: this feeds a page shown to people who have
    not proved they know it. `query_enrollment_key` is the one that reads the
    value, and it exists to compare it.
    """
    stmt = select(
        Course.c.id,
        Course.c.title,
        Course.c.description,
        (Course.c.enrollment_key.is_not(None)).label("requires_key"),
    ).where(Course.c.id == course_id)
    return (await conn.execute(stmt)).first()


async def query_enrollment_key(conn: AsyncConnection, course_id: int) -> str | None:
    """The course's enrollment key, or None if it has none and anyone may join.

    The only read of the column, and it is read to be compared against what a
    student typed - never to be rendered. Everything a page needs is
    `requires_key`.
    """
    stmt = select(Course.c.enrollment_key).where(Course.c.id == course_id)
    return (await conn.execute(stmt)).scalar_one_or_none()


# The challenges of one course, in the order the course page lists them, each
# carrying what the card on that page has to decide its badge with:
#
# - `n_mine`, how many times the viewer submitted, which is what a student's
#   "Submitted" badge means;
# - `n_submitters`, how many distinct people did, which is the same badge as
#   course staff see it - and what tells them which challenge the class has
#   actually started.
#
# Takes :course_id and :user_id. The ordering is `position, id`: `position` is
# what the arrows write, and the id breaks a tie, so challenges that have never
# been reordered stay in the order they were created in.
_COURSE_CHALLENGES_CTE = """
    submitted AS (
        SELECT submission.challenge_id,
               count(*) FILTER (WHERE submission.user_id = :user_id) AS n_mine,
               count(DISTINCT submission.user_id) AS n_submitters
        FROM submission
        JOIN challenge ON challenge.id = submission.challenge_id
        WHERE challenge.course_id = :course_id
        GROUP BY submission.challenge_id
    ),
    -- LEFT JOIN and coalesce: a challenge nobody has submitted to has no row
    -- in `submitted` at all, and it is still a challenge the page must list.
    challenges AS (
        SELECT challenge.*,
               coalesce(submitted.n_mine, 0) AS n_mine,
               coalesce(submitted.n_submitters, 0) AS n_submitters
        FROM challenge
        LEFT JOIN submitted ON submitted.challenge_id = challenge.id
        WHERE challenge.course_id = :course_id
    )
"""


async def query_course_info(conn: AsyncConnection, course_id: int, user_id: int):
    """The course, its description and its challenges as the page lists them.

    One document rather than a query per card: see `_COURSE_CHALLENGES_CTE` for
    what each challenge carries. The course editor reads the same thing - it
    shows the same list, plus the arrows - so every reorder re-renders from
    here and cannot drift from what is stored.
    """
    query = text(f"""
    WITH
    {_COURSE_CHALLENGES_CTE},
    c AS (
        -- The subquery and its coalesce, rather than a join and a GROUP BY: a
        -- course with no challenges yet is a real course - it is the one whose
        -- page course staff open to create the first challenge - and it must
        -- come back with an empty list, not as no row at all, which the route
        -- would report as a 404.
        SELECT
            course.*,
            coalesce(
                (SELECT json_agg(challenges ORDER BY challenges.position, challenges.id)
                 FROM challenges),
                '[]'::json
            ) as challenges
        FROM course
        WHERE course.id = :course_id
    )
    SELECT row_to_json(c) FROM c;
    """)
    res = await conn.execute(query, dict(course_id=course_id, user_id=user_id))
    return res.scalar()


async def query_course_challenges(conn: AsyncConnection, course_id: int, user_id: int):
    """Just the list `query_course_info` nests, for the fragment the reorder
    arrows swap: a move changes the order of the cards and nothing about the
    course itself."""
    query = text(f"""
    WITH
    {_COURSE_CHALLENGES_CTE}
    SELECT coalesce(
        json_agg(challenges ORDER BY challenges.position, challenges.id),
        '[]'::json
    )
    FROM challenges;
    """)
    res = await conn.execute(query, dict(course_id=course_id, user_id=user_id))
    return res.scalar()

