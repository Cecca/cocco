from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import and_, bindparam, delete, func, insert, select, text, true, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    LeaderboardRanking,
    OutcomeEnum,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
    UserRole,
)
from cocco.repository.judging import notify_queue
from cocco.repository.rules import (
    _EMPTY_JSON_ARRAY,
    _challenge_number,
    _challenge_visible,
    _effective_archived_at,
)


async def insert_submission(
    conn: AsyncConnection,
    user_id: int,
    challenge_id: int,
    language: str,
    filename: str,
    source: str,
) -> int:
    """Enqueue a new submission, and wake the judge for it. Like
    `insert_enrollment`, this does not commit: the caller owns the transaction,
    and the notification goes out when - and only if - it commits."""
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
    submission_id = (await conn.execute(stmt)).scalar_one()
    await notify_queue(conn)
    return submission_id


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


def _score_columns(outcomes):
    """A submission's headline numbers, as aggregates over rows of
    `testoutcome` - `outcomes` is that table or anything with its columns:
    the mean score, the total running time, how many tests passed and how many
    there were. Labelled, for a `select`. A missing score counts as zero, and
    the mean assumes a higher-is-better evaluator.

    `query_user_submissions` and `query_challenge_standings` spell the same
    four aggregates in their own `text()`; keep them in step.
    """
    return [
        func.avg(func.coalesce(outcomes.c.score, 0)).label("score"),
        func.sum(outcomes.c.elapsed_s).label("elapsed_s"),
        func.count()
        .filter(outcomes.c.outcome == OutcomeEnum.success)
        .label("n_passed"),
        func.count().label("n_tests"),
    ]


def _submission_outcomes(submission_id):
    """The outcomes of one submission, joined to the test cases they were
    scored on, as a CTE of exactly one row: the outcomes as a JSON array in
    execution order, beside `_score_columns`.

    Shared by the two queries below, which differ only in how much of the
    submission itself they carry: `is_sample` is what tells the page whether
    the free-text fields may be shown to a student (see `visible_outcomes` in
    `web/challenges.py`). Callers select `*agg.c` and join it `ON true`.
    """
    outcomes = (
        select(
            TestOutcome,
            TestCase.c.execution_order,
            TestCase.c.is_sample,
            TestCase.c.is_active,
        )
        .join(TestCase, TestCase.c.id == TestOutcome.c.testcase_id)
        .where(TestOutcome.c.submission_id == submission_id)
        .cte("outcomes")
    )
    # Aggregated with no GROUP BY, so this is one row even when the submission
    # has no outcomes yet and can be joined `ON true` unconditionally.
    agg = select(
        func.coalesce(
            func.json_agg(
                aggregate_order_by(
                    outcomes.table_valued(),
                    outcomes.c.execution_order,
                    outcomes.c.id,
                )
            ),
            _EMPTY_JSON_ARRAY,
        ).label("outcomes"),
        *_score_columns(outcomes),
    ).cte("agg")
    return agg


async def query_submission(conn: AsyncConnection, submission_id: int):
    """One submission in full: its source, who wrote it, where it belongs, and
    every outcome it was scored on.

    This is the only query that returns `source`, which is why the fragment that
    polls the same page uses `query_submission_outcomes` instead.

    `attempt` is the submission's position in its author's history on the
    challenge, computed exactly as `query_user_submissions` does it, so the "#3"
    on this page is the "#3" of the table the viewer clicked.
    """
    submission = bindparam("submission_id", submission_id)
    agg = _submission_outcomes(submission)
    # Every submission by the same author to the same challenge as this one.
    this = Submission.alias("this")
    attempts = (
        select(
            Submission.c.id,
            func.row_number().over(order_by=Submission.c.enqueued).label("attempt"),
        )
        .join(
            this,
            and_(
                this.c.user_id == Submission.c.user_id,
                this.c.challenge_id == Submission.c.challenge_id,
            ),
        )
        .where(this.c.id == submission)
        .cte("attempts")
    )
    s = (
        select(
            Submission,
            User.c.nickname.label("author_nickname"),
            User.c.email.label("author_email"),
            Challenge.c.title.label("challenge_title"),
            Challenge.c.course_id,
            Course.c.title.label("course_title"),
            attempts.c.attempt,
            *agg.c,
        )
        .select_from(
            Submission.join(User, User.c.id == Submission.c.user_id)
            .join(Challenge, Challenge.c.id == Submission.c.challenge_id)
            .outerjoin(Course, Course.c.id == Challenge.c.course_id)
            .outerjoin(attempts, attempts.c.id == Submission.c.id)
            .join(agg, true())
        )
        .where(Submission.c.id == submission)
        .cte("s")
    )
    stmt = select(func.row_to_json(s.table_valued()))
    return (await conn.execute(stmt)).scalar_one_or_none()


async def query_submission_outcomes(conn: AsyncConnection, submission_id: int):
    """What the submission page polls for: the status and the outcomes, and
    deliberately *not* the source, which never changes and can be a megabyte.

    `user_id` and `challenge_id` come along because the fragment route has to
    authorize the viewer just like the page does.
    """
    submission = bindparam("submission_id", submission_id)
    agg = _submission_outcomes(submission)
    s = (
        select(
            Submission.c.id,
            Submission.c.user_id,
            Submission.c.challenge_id,
            Submission.c.status,
            *agg.c,
        )
        .select_from(Submission.join(agg, true()))
        .where(Submission.c.id == submission)
        .cte("s")
    )
    stmt = select(func.row_to_json(s.table_valued()))
    return (await conn.execute(stmt)).scalar_one_or_none()


async def query_challenge(conn: AsyncConnection, user_id: int, challenge_id: int):
    """The challenge itself, its course, its number within the course, the
    viewer's role in that course and its public sample test cases.

    `number` is not `challenge.position`; see `_challenge_number`.

    Submissions are deliberately not part of this document: the challenge page
    loads them through `query_user_submissions` and keeps them fresh by polling.

    `effective_archived_at` is what decides whether the page shows a submit
    form; see `_effective_archived_at` for why it is not `challenge.archived_at`.

    `is_visible` is its neighbour and answers a different question - whether the
    class may see this at all. Only staff ever read a document whose `is_visible`
    is false, because `require_challenge_viewer` refuses everybody else before
    this query runs; it is here so the editor's form and the staff badge can say
    which of the three states the challenge is in. `visible_from` itself rides
    along in `challenge.*`.
    """
    challenge = bindparam("challenge_id", challenge_id)
    user = bindparam("user_id", user_id)
    sample = (
        select(TestCase.c.id, TestCase.c.execution_order)
        .where(
            TestCase.c.challenge_id == challenge,
            TestCase.c.is_sample,
            TestCase.c.is_active,
        )
        .subquery("sample")
    )
    samples = select(
        func.json_agg(
            aggregate_order_by(sample.table_valued(), sample.c.execution_order)
        )
    ).scalar_subquery()
    # Over every challenge, so the rank is the course's whatever this one is.
    numbers = select(Challenge.c.id, _challenge_number().label("number")).cte("numbers")
    cs = (
        select(
            Challenge,
            Course.c.title.label("course_title"),
            _effective_archived_at().label("effective_archived_at"),
            _challenge_visible().label("is_visible"),
            numbers.c.number,
            Enrollment.c.role.label("viewer_role"),
            func.coalesce(samples, _EMPTY_JSON_ARRAY).label("samples"),
        )
        .select_from(
            Challenge.outerjoin(Course, Course.c.id == Challenge.c.course_id)
            .outerjoin(numbers, numbers.c.id == Challenge.c.id)
            .outerjoin(
                Enrollment,
                and_(
                    Enrollment.c.course_id == Challenge.c.course_id,
                    Enrollment.c.user_id == user,
                ),
            )
        )
        .where(Challenge.c.id == challenge)
        .cte("cs")
    )
    stmt = select(func.row_to_json(cs.table_valued()))
    return (await conn.execute(stmt)).scalar_one_or_none()


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
    submissions. Returns how many were requeued, and if that is any, wakes the
    judge for them when the caller commits (`notify_queue`).

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
        # Both timestamps go back to NULL with the status: they describe the run
        # whose outcomes were just deleted, and leaving them would report a
        # queued submission as having finished.
        .values(status=SubmissionStatus.queued, started_at=None, finished_at=None)
    )
    if result.rowcount:
        await notify_queue(conn)
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


async def query_challenge_config(conn: AsyncConnection, challenge_id: int):
    """A challenge's own row, plain Core.

    Deliberately not `query_challenge`, which is the *page* query: that one
    needs a viewer to compute their role, joins five tables and returns one
    nested document. The transfer format wants the columns and nothing else.
    """
    stmt = select(Challenge).where(Challenge.c.id == challenge_id)
    return (await conn.execute(stmt)).first()


async def query_editable_challenges(
    conn: AsyncConnection,
    user_id: int,
    editor_roles: Iterable[UserRole],
    every_course: bool = False,
):
    """Every challenge the caller may edit, with the course it belongs to.

    This is what `cocco ls` prints, and the only thing that makes an id
    discoverable from a command line. Plain Core rather than the `json_agg`
    shape the page queries use, deliberately: a list of ids and titles is a row
    set, and nothing here renders a page.

    `editor_roles` is passed in rather than spelled here so that `EDITOR_ROLES`
    stays the one definition of course staff, and `every_course` is how a site
    administrator - who is staff everywhere without an enrollment - asks the
    same question. Ordered the way both editors order: `position, id` within a
    course, courses by title.

    `archived_at` here is the *effective* one - `_effective_archived_at` - so
    `cocco ls` marks a challenge whose course was archived as well as one
    archived on its own. A push to either is still accepted; see
    `api_import_challenge`.

    `visible_from` and `is_visible` come back for the same reason and with the
    same force: a note beside the title, never anything missing from the list.
    This is a listing of what the caller may *edit*, and hiding a challenge is
    precisely what staff do while they are still writing it - a `cocco ls` that
    dropped them would hide the ids of the challenges most likely to be pushed
    to.
    """
    joined = Challenge.join(Course, Challenge.c.course_id == Course.c.id)
    stmt = select(
        Course.c.id.label("course_id"),
        Course.c.title.label("course_title"),
        Challenge.c.id.label("challenge_id"),
        Challenge.c.title,
        _effective_archived_at().label("archived_at"),
        Challenge.c.visible_from,
        _challenge_visible().label("is_visible"),
    )
    if every_course:
        stmt = stmt.select_from(joined)
    else:
        stmt = stmt.select_from(
            joined.join(
                Enrollment,
                and_(
                    Enrollment.c.course_id == Course.c.id,
                    Enrollment.c.user_id == user_id,
                ),
            )
        ).where(Enrollment.c.role.in_(list(editor_roles)))
    stmt = stmt.order_by(
        Course.c.title, Course.c.id, Challenge.c.position, Challenge.c.id
    )
    return (await conn.execute(stmt)).all()


async def insert_challenge(
    conn: AsyncConnection,
    course_id: int,
    title: str,
    description: str,
    max_time_s: float,
    max_memory_bytes: int,
    max_threads: int,
    ranking: LeaderboardRanking,
    visible_from: datetime | None = None,
) -> int:
    """Create a challenge on a course and return its id.

    It lands after the ones already there, exactly as `insert_testcase` appends
    within a challenge: a new challenge is the next one of the course, and the
    arrows in the course editor are for moving it somewhere else.

    `evaluator_path` is left NULL on purpose: a brand new challenge has chosen
    no checker, and the judge grades those with the bundled `equality.py`.

    `visible_from` defaults to NULL for the same kind of reason, and it is the
    one default here that changes what the class sees: a brand new challenge is
    *hidden*. It has no test cases and no evaluator either, so a course page
    that listed it the instant it was created was advertising a page that could
    not be solved. The create form offers the three states all the same, so
    staff who want it live at once can say so.
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
            visible_from=visible_from,
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
