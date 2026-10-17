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
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.schema import SubmissionRunData
from cocco.model import (
    Administrator,
    ApiToken,
    Challenge,
    Course,
    Enrollment,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSession,
    ExerciseSet,
    ExerciseSetVersion,
    ExerciseVersionStatus,
    LeaderboardRanking,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
    UserPassword,
    UserRole,
)


# A challenge is archived when it says so, or when its course does. That `or`
# is the whole of "archiving a course archives its challenges", and it is
# written here twice and nowhere else: once as SQL, because the page queries
# are raw text, and once as a Core expression, because the routes that need the
# answer for one row read the rule rather than re-deriving it. Both are named,
# both point at each other, and a test asserts they agree.
#
# Nothing downstream - no route, no template - may spell it a third time, the
# way nothing downstream spells `effective_course_role`'s promotion again. A
# template that reads `challenge.archived_at` for a badge silently un-archives
# every challenge under an archived course.
_EFFECTIVE_ARCHIVED_SQL = "coalesce(challenge.archived_at, course.archived_at)"


def _effective_archived_at():
    """`_EFFECTIVE_ARCHIVED_SQL` as a Core expression. Only valid in a
    statement that has both tables in its FROM."""
    return func.coalesce(Challenge.c.archived_at, Course.c.archived_at)


# Whether a challenge is open to the class yet: `challenge.visible_from` is
# NULL for a hidden one, a past instant for a live one and a future one for a
# challenge that opens by itself at that moment.
#
# The neighbour of `_EFFECTIVE_ARCHIVED_SQL` above and written under the same
# discipline: twice and nowhere else, once as SQL because the page queries are
# raw text and once as a Core expression for the single-row reads a gate makes.
# Nothing downstream may spell it a third time.
#
# Two differences from its neighbour. It does not cascade from the course - a
# course has no hidden state to cascade, so this needs only `challenge` in its
# FROM - and it is a *boolean* rather than a timestamp, because "when" and
# "whether" come apart here: a page that has to say "opens on the 1st" reads
# `visible_from` itself, and a gate that has to decide reads this.
#
# `now()` and never a Python clock, for the reason `model.py` gives for every
# timestamp in the schema: the transaction's clock is the one that counts.
_CHALLENGE_VISIBLE_SQL = (
    "(challenge.visible_from IS NOT NULL AND challenge.visible_from <= now())"
)


def _challenge_visible():
    """`_CHALLENGE_VISIBLE_SQL` as a Core expression."""
    return and_(
        Challenge.c.visible_from.is_not(None),
        Challenge.c.visible_from <= func.now(),
    )


# The channel the web app tells the judge about new work on. `notify_queue` is
# the one place it is sent from and `cocco.judge` the one place it is listened
# on, so both ends read this name rather than spelling it.
QUEUE_CHANNEL = "cocco_queue"


async def notify_queue(conn: AsyncConnection) -> None:
    """Tell any listening judge there is something queued. Does not commit.

    `pg_notify` inside the caller's transaction, deliberately: PostgreSQL holds
    a notification until its transaction commits and drops it on a rollback, so
    a judge woken by it always finds the row it was woken for - never a wake-up
    ahead of a row it cannot see yet, and never one for a row that did not
    happen. It carries no payload: the judge's answer to it is the same claim
    it makes on every poll, and the poll stays as the fallback for a
    notification nobody was listening for.
    """
    await conn.execute(select(func.pg_notify(QUEUE_CHANNEL, "")))


async def pull_queued_submission(conn: Connection) -> SubmissionRunData | None:
    """Get a queued submission, along with all the data required to run it.
    Atomically set it to 'running'."""
    # Get the submission to run, if any, and atomically sets it to running.
    #
    # `FOR UPDATE SKIP LOCKED` is what makes that "atomically" true with more
    # than one claimer, and the judge now has several. Without it, a second
    # claimer's subquery picks the row the first has locked, its UPDATE waits
    # for the first to commit, and then - the subquery is not evaluated again -
    # it claims the same row a second time: one submission, graded twice. With
    # it, the second claimer passes over the locked row and takes the next one,
    # and never waits on another claimer at all.
    submission = conn.execute(
        update(Submission)
        .where(
            # Redundant with the lock below, and kept as the statement's own
            # promise: whatever the subquery picked, only a queued row is taken.
            Submission.c.status == SubmissionStatus.queued,
            Submission.c.id
            == (
                select(Submission.c.id)
                .where(Submission.c.status == SubmissionStatus.queued)
                .order_by(Submission.c.enqueued)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_subquery()
        )
        .values(status=SubmissionStatus.running, started_at=func.now())
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
        filename=submission.filename,
        language=submission.language,
        max_threads=challenge.max_threads,
        max_memory_bytes=challenge.max_memory_bytes,
        max_time_s=challenge.max_time_s,
        test_cases=[schema.TestCase.model_validate(t) for t in tests],
        evaluator_path=challenge.evaluator_path,
        challenge_id=challenge.id,
    )


def requeue_submission(conn: Connection, submission_id: int) -> None:
    """Put a claimed submission back on the queue, as if never picked up.

    For a judge stopped mid-run. Only while it is still `running`, so a
    submission that was meanwhile regraded, finished or deleted is left alone.
    """
    conn.execute(
        update(Submission)
        .where(
            and_(
                Submission.c.id == submission_id,
                Submission.c.status == SubmissionStatus.running,
            )
        )
        .values(status=SubmissionStatus.queued, started_at=None)
    )


def mark_submission_done(conn: Connection, submission_id: int) -> None:
    conn.execute(
        update(Submission)
        .where(Submission.c.id == submission_id)
        .values(status=SubmissionStatus.done, finished_at=func.now())
    )


def insert_test_outcomes(conn: Connection, outcomes: list[schema.TestOutcome]) -> None:
    if not outcomes:
        return
    conn.execute(
        insert(TestOutcome),
        [o.model_dump(exclude={"id"}) for o in outcomes],
    )


# --- Exercise jobs, the judge's side ------------------------------------------
#
# Sync and on a `Connection`, like the submission helpers above. The web app's
# side - creating the jobs and reading their results - is async below.


def pull_queued_exercise_job(
    conn: Connection, kind: ExerciseJobKind
) -> schema.ExerciseJobRunData | None:
    """Claim the oldest queued exercise job of one kind, and commit the claim.

    One kind per call, because the kind *is* the priority: the judge asks for
    a run first, then a check, then a build (`judge.QUEUES`), so one query that
    took "the most urgent of anything" would put the priority in SQL that the
    challenge queue could not join. `FOR UPDATE SKIP LOCKED` for
    `pull_queued_submission`'s reason, which `ix_exercise_job_queue` serves.
    """
    job = conn.execute(
        update(ExerciseJob)
        .where(
            ExerciseJob.c.status == ExerciseJobStatus.queued,
            ExerciseJob.c.id
            == (
                select(ExerciseJob.c.id)
                .where(
                    ExerciseJob.c.status == ExerciseJobStatus.queued,
                    ExerciseJob.c.kind == kind,
                )
                .order_by(ExerciseJob.c.enqueued, ExerciseJob.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_subquery(),
        )
        .values(status=ExerciseJobStatus.running, started_at=func.now())
        .returning(*ExerciseJob.c)
    ).first()
    if job is None:
        return None
    conn.commit()
    return schema.ExerciseJobRunData.model_validate(job)


def query_job_exercise(
    conn: Connection, version_id: int, exercise_index: int
) -> tuple[str, schema.Exercise] | None:
    """The language and the exercise a check or a run is about, in one query.

    Straight out of the version's compiled document, tests and expected
    outputs included: exercise inputs are a few bytes an author typed, so they
    live in the database rather than in `Storage`. None for a version with no
    document - one still building, or failed - or with no such exercise.
    """
    row = conn.execute(
        select(
            ExerciseSetVersion.c.document["language"].astext,
            ExerciseSetVersion.c.document["exercises"][exercise_index - 1],
        ).where(ExerciseSetVersion.c.id == version_id)
    ).first()
    if row is None or row[1] is None:
        return None
    return row[0], schema.Exercise.model_validate(row[1])


def query_version_to_build(conn: Connection, version_id: int):
    """A version's source and title, which is all a build starts from."""
    return conn.execute(
        select(
            ExerciseSetVersion.c.source,
            ExerciseSetVersion.c.title,
        ).where(ExerciseSetVersion.c.id == version_id)
    ).first()


def finish_version_build(
    conn: Connection,
    version_id: int,
    status: ExerciseVersionStatus,
    problems: list,
    document: dict | None,
) -> bool:
    """Write what a build found, once, and make a ready version current.

    The three columns in one `UPDATE`, and only while the version is still
    `building`: a version is written once, by the build that finishes it, so a
    second build of it - a requeued one that did not know the first finished -
    changes nothing. Answers whether it was this call that wrote them.

    On `ready` the set's `current_version_id` moves in the same transaction,
    and only *forward*: two imports in quick succession are two builds that
    may finish in either order, and the older finishing second must not take
    the set back to itself. Ids come out of one sequence, so "newer" is "larger".
    """
    set_id = conn.execute(
        update(ExerciseSetVersion)
        .where(
            ExerciseSetVersion.c.id == version_id,
            ExerciseSetVersion.c.status == ExerciseVersionStatus.building,
        )
        .values(status=status, problems=problems, document=document)
        .returning(ExerciseSetVersion.c.set_id)
    ).scalar_one_or_none()
    if set_id is None:
        return False
    if status is ExerciseVersionStatus.ready:
        conn.execute(
            update(ExerciseSet)
            .where(
                ExerciseSet.c.id == set_id,
                or_(
                    ExerciseSet.c.current_version_id.is_(None),
                    ExerciseSet.c.current_version_id < version_id,
                ),
            )
            .values(current_version_id=version_id)
        )
    return True


def finish_exercise_job(conn: Connection, job_id: int, result: dict) -> None:
    """Record a job's result. Zero rows when the job was deleted meanwhile -
    its session, its set or its course - which is harmless by design: the
    result is a column of the row, not a row of its own that a foreign key
    could refuse."""
    conn.execute(
        update(ExerciseJob)
        .where(
            ExerciseJob.c.id == job_id,
            ExerciseJob.c.status == ExerciseJobStatus.running,
        )
        .values(status=ExerciseJobStatus.done, result=result, finished_at=func.now())
    )


def requeue_exercise_job(conn: Connection, job_id: int, superseded: dict) -> bool:
    """`requeue_submission` for an exercise job: a judge stopped mid-run puts
    it back, as if never picked up, while it is still `running`. Answers
    whether it did.

    It cannot always: while this one ran, its owner may have pressed the same
    button again, and `uq_exercise_job_one_queued` allows one queued job per
    button. Then the newer job is the one that counts, and this one is finished
    with `superseded` as its result instead. The requeue is tried inside a
    savepoint, rather than after a check for the sibling, so a sibling queued
    between the two cannot turn this into an IntegrityError.
    """
    try:
        with conn.begin_nested():
            result = conn.execute(
                update(ExerciseJob)
                .where(
                    ExerciseJob.c.id == job_id,
                    ExerciseJob.c.status == ExerciseJobStatus.running,
                )
                .values(status=ExerciseJobStatus.queued, started_at=None)
            )
        return bool(result.rowcount)
    except IntegrityError:
        finish_exercise_job(conn, job_id, superseded)
        return False


# How long a run job is kept: a term, so a teacher can see what a student tried
# before they dared press Check, and the same six months the logs are kept for.
# Checks are never pruned - progress is derived from them.
RUN_JOB_RETENTION = "6 months"


def prune_run_jobs(conn: Connection, limit: int) -> int:
    """Delete up to `limit` finished run jobs older than `RUN_JOB_RETENTION`,
    and say how many. The judge calls it, in batches, when it is idle."""
    result = conn.execute(
        delete(ExerciseJob).where(
            ExerciseJob.c.id.in_(
                select(ExerciseJob.c.id)
                .where(
                    ExerciseJob.c.status == ExerciseJobStatus.done,
                    ExerciseJob.c.kind == ExerciseJobKind.run,
                    ExerciseJob.c.enqueued
                    < func.now() - text(f"interval '{RUN_JOB_RETENTION}'"),
                )
                .limit(limit)
            )
        )
    )
    return result.rowcount

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
        # `updated_at` is named here and nowhere else in this module: SQLAlchemy
        # fires a column's `onupdate` for an UPDATE and not for the DO UPDATE of
        # an upsert, so this is the one write that has to say it out loud.
        set_={"role": role, "updated_at": func.now()},
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


async def insert_exercise_job(
    conn: AsyncConnection,
    *,
    kind: ExerciseJobKind,
    user_id: int,
    version_id: int,
    session_id: int | None = None,
    exercise_index: int | None = None,
    source: str | None = None,
    stdin: str | None = None,
) -> int:
    """Enqueue an exercise job, and wake the judge for it. Does not commit,
    for `insert_submission`'s reason. Answers the id of the job that is now
    queued for this button.

    The repeated-button rule lives here, as one upsert on
    `uq_exercise_job_one_queued`: when the same person already has the same
    kind of job queued on the same exercise of the same session - or preview -
    the new code and stdin *replace* that job's, and its id is the answer. The
    row keeps its `enqueued`, so mashing the button neither builds a backlog
    nor sends its owner to the back of the queue, and a tab still polling the
    old id finds the newest request's result there. A job that is already
    `running` is outside the index's predicate, so it is never touched: the
    new one is queued beside it. A build is outside the predicate as well, and
    is always a new row.

    An upsert rather than a delete-then-insert, so two clicks that race cannot
    both win, and rather than a check before the insert, so a judge claiming
    the queued row in between cannot turn the replacement into a second queued
    job: PostgreSQL decides "conflict or not" against the committed claim.
    """
    stmt = pg_insert(ExerciseJob).values(
        kind=kind,
        status=ExerciseJobStatus.queued,
        user_id=user_id,
        version_id=version_id,
        session_id=session_id,
        exercise_index=exercise_index,
        source=source,
        stdin=stdin,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[
            ExerciseJob.c.user_id,
            ExerciseJob.c.kind,
            ExerciseJob.c.session_id,
            ExerciseJob.c.version_id,
            ExerciseJob.c.exercise_index,
        ],
        # The index's own predicate, which is what lets PostgreSQL infer the
        # partial index as the arbiter.
        index_where=text("status = 'queued' AND kind <> 'build'"),
        set_={"source": stmt.excluded.source, "stdin": stmt.excluded.stdin},
    ).returning(ExerciseJob.c.id)
    job_id = (await conn.execute(stmt)).scalar_one()
    await notify_queue(conn)
    return job_id


# --- Exercise sets, the web app's side ----------------------------------------


async def exercise_set_in_course(
    conn: AsyncConnection, set_id: int, course_id: int
) -> bool:
    """`challenge_in_course` for an exercise set: the role comes from the
    course in the URL, so the set in it is checked against that course rather
    than trusted."""
    stmt = select(ExerciseSet.c.id).where(
        ExerciseSet.c.id == set_id, ExerciseSet.c.course_id == course_id
    )
    return (await conn.execute(stmt)).first() is not None


async def insert_exercise_set(conn: AsyncConnection, course_id: int) -> int:
    """A set with no version yet. Does not commit: an import creates the set,
    its first version and the build in one transaction, so a file that fails
    half-way leaves no empty set behind."""
    stmt = insert(ExerciseSet).values(course_id=course_id).returning(ExerciseSet.c.id)
    return (await conn.execute(stmt)).scalar_one()


async def insert_exercise_version(
    conn: AsyncConnection, set_id: int, source: str, title: str
) -> int:
    """A `building` version of a set: the source verbatim, the title from its
    front matter. The build that finishes it is queued by the caller, in the
    same transaction. Does not commit."""
    stmt = (
        insert(ExerciseSetVersion)
        .values(
            set_id=set_id,
            source=source,
            title=title,
            status=ExerciseVersionStatus.building,
        )
        .returning(ExerciseSetVersion.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


# Each version of the set, newest first, with what the set page shows about it:
# its status, how many problems of each level it has and - once built - how
# many exercises. `number` is its rank within the set, oldest first, so "v3"
# means the third import of *this* set whatever the ids are. The problems
# themselves are not listed, only counted: the page prints the latest version's
# in full and nobody else's, and the fragment carrying this is polled.
_EXERCISE_VERSIONS_CTE = """
    versions AS (
        SELECT v.id, v.set_id, v.title, v.status, v.created_at,
               row_number() OVER (PARTITION BY v.set_id ORDER BY v.id) AS number,
               jsonb_array_length(v.document -> 'exercises') AS n_exercises,
               (SELECT count(*) FROM jsonb_array_elements(v.problems) p
                WHERE p ->> 'level' = 'error') AS n_errors,
               (SELECT count(*) FROM jsonb_array_elements(v.problems) p
                WHERE p ->> 'level' = 'warning') AS n_warnings
        FROM exercise_set_version v
        JOIN exercise_set ON exercise_set.id = v.set_id
        WHERE {where}
    )
"""


# A set's title, which is its current version's: a failed re-import must not
# rename the set it failed to change, on the course page or in the delete
# dialog. The latest version's only while there is no current one - a set whose
# first build has not succeeded yet still needs a name. Takes the `current` and
# `latest` rows of `_EXERCISE_VERSIONS_CTE`.
_EXERCISE_SET_TITLE_SQL = "coalesce(current.title, latest.title)"


async def query_exercise_sets(conn: AsyncConnection, course_id: int):
    """The exercise sets of a course, as its page lists them for staff.

    The title and the exercise count are the current version's - see
    `_EXERCISE_SET_TITLE_SQL` - and the status badge is the latest's, which is
    what tells its author a re-import is building or broke.
    """
    query = text(f"""
    WITH
    {_EXERCISE_VERSIONS_CTE.format(where="exercise_set.course_id = :course_id")},
    sets AS (
        SELECT exercise_set.id, exercise_set.archived_at,
               exercise_set.current_version_id,
               {_EXERCISE_SET_TITLE_SQL} AS title,
               latest.status AS latest_status,
               latest.number AS latest_number,
               current.number AS current_number,
               current.n_exercises
        FROM exercise_set
        JOIN LATERAL (
            SELECT * FROM versions WHERE versions.set_id = exercise_set.id
            ORDER BY versions.id DESC LIMIT 1
        ) latest ON true
        LEFT JOIN versions current ON current.id = exercise_set.current_version_id
        WHERE exercise_set.course_id = :course_id
    )
    SELECT coalesce(json_agg(sets ORDER BY sets.id), '[]'::json) FROM sets;
    """)
    return (await conn.execute(query, dict(course_id=course_id))).scalar()


async def query_exercise_set(conn: AsyncConnection, set_id: int):
    """The set page's document: the set and its title, every version of it
    newest first, the latest version's problems in full, whether any version
    is still building, and how many sessions it has.

    No version's source or document: the page that polls this shows statuses,
    and the preview reads a document through `query_exercise_version`.
    """
    query = text(f"""
    WITH
    {_EXERCISE_VERSIONS_CTE.format(where="v.set_id = :set_id")},
    latest AS (
        SELECT versions.*, v.problems
        FROM versions JOIN exercise_set_version v USING (id)
        ORDER BY versions.id DESC LIMIT 1
    ),
    s AS (
        SELECT exercise_set.*,
               {_EXERCISE_SET_TITLE_SQL} AS title,
               row_to_json(latest) AS latest,
               -- Any, not only the latest: two imports in quick succession are
               -- two builds, and the older one finishing is news too.
               EXISTS (SELECT 1 FROM versions WHERE status = 'building')
                   AS any_building,
               coalesce(
                   (SELECT json_agg(versions ORDER BY versions.id DESC) FROM versions),
                   '[]'::json
               ) AS versions,
               (SELECT count(*) FROM exercise_session
                WHERE exercise_session.set_id = exercise_set.id) AS n_sessions
        FROM exercise_set
        JOIN latest ON true
        LEFT JOIN versions current ON current.id = exercise_set.current_version_id
        WHERE exercise_set.id = :set_id
    )
    SELECT row_to_json(s) FROM s;
    """)
    return (await conn.execute(query, dict(set_id=set_id))).scalar()


async def query_exercise_version(conn: AsyncConnection, set_id: int, version_id: int):
    """One version of one set - the set is part of the key, so a version id
    from another set is not found - with its compiled document, or None."""
    stmt = select(
        ExerciseSetVersion.c.id,
        ExerciseSetVersion.c.title,
        ExerciseSetVersion.c.status,
        ExerciseSetVersion.c.document,
    ).where(
        ExerciseSetVersion.c.id == version_id,
        ExerciseSetVersion.c.set_id == set_id,
    )
    return (await conn.execute(stmt)).first()


async def query_exercise_version_source(
    conn: AsyncConnection, set_id: int, version_id: int
):
    """A version's file as it was imported, with its title and its `number`
    within the set to name the download by."""
    earlier = ExerciseSetVersion.alias("earlier")
    number = (
        select(func.count())
        .select_from(earlier)
        .where(earlier.c.set_id == set_id, earlier.c.id <= version_id)
        .scalar_subquery()
    )
    stmt = select(
        ExerciseSetVersion.c.source,
        ExerciseSetVersion.c.title,
        number.label("number"),
    ).where(
        ExerciseSetVersion.c.id == version_id,
        ExerciseSetVersion.c.set_id == set_id,
    )
    return (await conn.execute(stmt)).first()


async def query_exercise(
    conn: AsyncConnection, version_id: int, exercise_index: int
) -> schema.Exercise | None:
    """`query_job_exercise` for the web app: one exercise out of a version's
    document, by its 1-based position, or None."""
    stmt = select(
        ExerciseSetVersion.c.document["exercises"][exercise_index - 1]
    ).where(ExerciseSetVersion.c.id == version_id)
    exercise = (await conn.execute(stmt)).scalar_one_or_none()
    return None if exercise is None else schema.Exercise.model_validate(exercise)


async def query_preview_job(
    conn: AsyncConnection, set_id: int, job_id: int, user_id: int
):
    """A staff preview job, for the fragment that polls it - only if it is a
    preview (no session), of a version of this set, and its asker's own.

    Each of the three is a key rather than a check after the read, so any job
    that is not all of them is simply not found: a check recorded in a session
    is never readable through a preview URL, and a polling tab never finds
    another person's job in its place.
    """
    stmt = (
        select(
            ExerciseJob.c.id,
            ExerciseJob.c.kind,
            ExerciseJob.c.status,
            ExerciseJob.c.version_id,
            ExerciseJob.c.exercise_index,
            ExerciseJob.c.result,
        )
        .join(ExerciseSetVersion, ExerciseSetVersion.c.id == ExerciseJob.c.version_id)
        .where(
            ExerciseJob.c.id == job_id,
            ExerciseJob.c.user_id == user_id,
            ExerciseJob.c.session_id.is_(None),
            ExerciseJob.c.kind != ExerciseJobKind.build,
            ExerciseSetVersion.c.set_id == set_id,
        )
    )
    return (await conn.execute(stmt)).first()


async def lock_exercise_set(conn: AsyncConnection, set_id: int) -> int | None:
    """Lock a set for deletion and say how many sessions it has, or None if it
    does not exist. Does not commit: the lock is held until the caller's
    transaction ends.

    The versions first, then the set - the order the judge takes them in when
    a build finishes (`finish_version_build` writes the version, then moves the
    set's `current_version_id`), so a delete and a finishing build queue up
    behind each other instead of deadlocking. Locking the versions also makes a
    preview being enqueued on one of them wait, or be waited for: its foreign
    key holds a share lock on the version row.

    Opening a session on the set has to take the set's row lock too, or a
    session could be opened between this count and the delete.

    One window stays open: a version imported between the two locks is not
    covered by the first. The deletes still take it - each statement sees what
    has committed - and only if a judge is finishing *that* version's build in
    the same instant can the two deadlock, which PostgreSQL ends by aborting
    one side; either way nothing is left half-done.
    """
    await conn.execute(
        select(ExerciseSetVersion.c.id)
        .where(ExerciseSetVersion.c.set_id == set_id)
        .with_for_update()
    )
    n_sessions = (
        select(func.count())
        .select_from(ExerciseSession)
        .where(ExerciseSession.c.set_id == set_id)
        .scalar_subquery()
    )
    stmt = (
        select(n_sessions)
        .select_from(ExerciseSet)
        .where(ExerciseSet.c.id == set_id)
        .with_for_update(of=ExerciseSet)
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


async def delete_exercise_set(conn: AsyncConnection, set_id: int) -> None:
    """Delete a set with no sessions, its versions and every job on them.

    Hand-ordered for `delete_challenge`'s reason: jobs, then the set's
    `current_version_id` set to NULL - the set and its versions point at each
    other - then the versions, then the set. The jobs go **by `version_id`**:
    a build and a preview have no session, and would otherwise hold the
    versions down. A judge finishing one of them meanwhile updates zero rows,
    which is why there is nothing here to wait for - the result is a column of
    the job, not a row of its own a foreign key could refuse.

    The caller has taken `lock_exercise_set` and found no sessions: a set with
    sessions is refused, not cascaded, since a session's checks are a class's
    work.
    """
    version_ids = select(ExerciseSetVersion.c.id).where(
        ExerciseSetVersion.c.set_id == set_id
    )
    await conn.execute(delete(ExerciseJob).where(ExerciseJob.c.version_id.in_(version_ids)))
    await conn.execute(
        update(ExerciseSet)
        .where(ExerciseSet.c.id == set_id)
        .values(current_version_id=None)
    )
    await conn.execute(
        delete(ExerciseSetVersion).where(ExerciseSetVersion.c.set_id == set_id)
    )
    await conn.execute(delete(ExerciseSet).where(ExerciseSet.c.id == set_id))


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

    `effective_archived_at` is what decides whether the page shows a submit
    form; see `_EFFECTIVE_ARCHIVED_SQL` for why it is not `challenge.archived_at`.

    `is_visible` is its neighbour and answers a different question - whether the
    class may see this at all. Only staff ever read a document whose `is_visible`
    is false, because `require_challenge_viewer` refuses everybody else before
    this query runs; it is here so the editor's form and the staff badge can say
    which of the three states the challenge is in. `visible_from` itself rides
    along in `challenge.*`.
    """
    query = text(f"""
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
               {_EFFECTIVE_ARCHIVED_SQL} AS effective_archived_at,
               {_CHALLENGE_VISIBLE_SQL} AS is_visible,
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


async def record_sign_in(conn: AsyncConnection, user_id: int) -> None:
    """Stamp a sign-in. Does not commit.

    One statement, and the `coalesce` is why: `first_login_at` is written by the
    first sign-in and by no later one, with no read for two tabs to race on.

    It is not `user.created_at`, and the difference is the point of the column.
    `get_or_create_user` is also how course staff pre-enrol somebody from the
    people page, so a row can exist for an address whose owner has never been
    near the login page - NULL here is exactly that person, and `created_at`
    could not tell you.

    Called from `_complete_sign_in` *after* the blacklist refusal: a sign-in
    that was turned away is not a sign-in, and putting it there is what keeps
    that true for both ways in.
    """
    await conn.execute(
        update(User)
        .where(User.c.id == user_id)
        .values(
            first_login_at=func.coalesce(User.c.first_login_at, func.now()),
            last_login_at=func.now(),
        )
    )


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
        .values(blocked_at=func.now(), blocked_reason=reason)
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


# ---------------------------------------------------------------------------
# Personal access tokens. The token itself never reaches this module: what is
# stored, and what is looked up, is `hash_token`'s output - see `cocco/tokens.py`.


async def insert_api_token(
    conn: AsyncConnection, user_id: int, label: str, token_hash: str
) -> int:
    """Record a freshly minted token and return its id. Does not commit."""
    stmt = (
        insert(ApiToken)
        .values(user_id=user_id, label=label, token_hash=token_hash)
        .returning(ApiToken.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


async def query_api_token_user(conn: AsyncConnection, token_hash: str):
    """Whose token this is, as the same row `query_user` returns.

    `_USER_COLUMNS` rather than a fresh column list, deliberately: that constant
    exists so that a door which forgot `blocked_at` cannot be the one somebody
    walks through, and this is the third door.
    """
    stmt = (
        select(*_USER_COLUMNS)
        .select_from(ApiToken.join(User, ApiToken.c.user_id == User.c.id))
        .where(ApiToken.c.token_hash == token_hash)
    )
    return (await conn.execute(stmt)).first()


async def touch_api_token(conn: AsyncConnection, token_hash: str) -> None:
    """Record that a token was just used. Does not commit."""
    await conn.execute(
        update(ApiToken)
        .where(ApiToken.c.token_hash == token_hash)
        .values(last_used_at=func.now())
    )


async def query_api_tokens(conn: AsyncConnection, user_id: int):
    """A user's tokens, newest first - never the hash.

    The column list is spelled out rather than `select(ApiToken)` for the reason
    `query_courses` lists its columns: the one value in this table that must not
    reach a page is the one a `SELECT *` would carry along.
    """
    stmt = (
        select(
            ApiToken.c.id,
            ApiToken.c.label,
            ApiToken.c.created_at,
            ApiToken.c.last_used_at,
        )
        .where(ApiToken.c.user_id == user_id)
        .order_by(ApiToken.c.created_at.desc(), ApiToken.c.id.desc())
    )
    return (await conn.execute(stmt)).all()


async def delete_api_token(
    conn: AsyncConnection, user_id: int, token_id: int
) -> bool:
    """Revoke a token. Does not commit. True if it did.

    Scoped to the owner in the WHERE clause rather than by a lookup first, the
    way `query_testcase` scopes a test case to its challenge: an id belonging to
    somebody else's account then names no row here, instead of naming one that a
    check has to remember to reject.
    """
    stmt = (
        delete(ApiToken)
        .where(and_(ApiToken.c.id == token_id, ApiToken.c.user_id == user_id))
        .returning(ApiToken.c.id)
    )
    return (await conn.execute(stmt)).first() is not None


# ---------------------------------------------------------------------------
# Passwords, for the deployments that turn `[auth] password_login` on. As with
# tokens, the password itself never reaches this module: what is stored is
# `hash_password`'s output - see `cocco/passwords.py`.


async def query_password_hash(conn: AsyncConnection, email: str) -> str | None:
    """The stored hash for an address, or None.

    None for two different situations on purpose - nobody has that address, and
    that account has no password - because the sign-in route must not be able
    to tell them apart, and neither must whoever is guessing at it.
    `verify_password` takes `str | None` for exactly this: the route hands
    whatever comes back straight to it, and every wrong answer costs the same
    work and produces the same sentence.

    A join and not a lookup on `user_password` alone, because the address is
    what a person types and `user.email` is where it lives.
    """
    stmt = (
        select(UserPassword.c.password_hash)
        .select_from(
            User.join(UserPassword, UserPassword.c.user_id == User.c.id)
        )
        .where(User.c.email == email)
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


async def claim_user_password(
    conn: AsyncConnection, email: str, password_hash: str
) -> bool:
    """Give an unclaimed account its first password. Does not commit. True if
    it did.

    This is registration, and it is the one statement the whole feature's
    safety rests on, so the rule is *in* it rather than in a check before it -
    `block_user` refusing an administrator in its own WHERE clause is the
    precedent, and the reason is the same: two requests racing must not be able
    to pass a check each and then both write.

    The rule is `first_login_at IS NULL`, and that column means precisely "this
    account has never been near the login page" - `get_or_create_user` leaves
    it NULL for a roster a teacher writes out before term, and `record_sign_in`
    sets it the first time somebody actually signs in. So an address a teacher
    has enrolled can be claimed by its owner, and an account somebody has
    already used cannot be taken over. `ON CONFLICT DO NOTHING` is the other
    half: an account that already has a password is never re-claimed, whatever
    its `first_login_at` says.

    That leaves exactly one thing a claim proves - that whoever made it can
    type an address `allowed_domains` admits - which is why this is off by
    default and why `cocco.example.toml` says so at length.
    """
    stmt = (
        pg_insert(UserPassword)
        .from_select(
            ["user_id", "password_hash"],
            select(User.c.id, literal(password_hash)).where(
                and_(User.c.email == email, User.c.first_login_at.is_(None))
            ),
        )
        .on_conflict_do_nothing(index_elements=[UserPassword.c.user_id])
        .returning(UserPassword.c.user_id)
    )
    return (await conn.execute(stmt)).first() is not None


async def set_user_password(
    conn: AsyncConnection, user_id: int, password_hash: str
) -> None:
    """Set or replace an account's password. Does not commit.

    An upsert, and it names `updated_at` itself: SQLAlchemy fires the column's
    `onupdate` for an UPDATE and not for the DO UPDATE of an upsert, which is
    the trap `set_enrollment_role` already documents.
    """
    stmt = (
        pg_insert(UserPassword)
        .values(user_id=user_id, password_hash=password_hash)
        .on_conflict_do_update(
            index_elements=[UserPassword.c.user_id],
            set_={"password_hash": password_hash, "updated_at": func.now()},
        )
    )
    await conn.execute(stmt)


async def delete_user_password(conn: AsyncConnection, user_id: int) -> bool:
    """Take an account's password away. Does not commit. True if there was one.

    Nothing else goes with it: the account, its enrollments and its submissions
    stay exactly where they are, and if Google is configured its owner signs in
    there tomorrow. This is the app's usual line - a block does not retract
    work, `delete_enrollment` takes one row - applied to a credential.
    """
    stmt = (
        delete(UserPassword)
        .where(UserPassword.c.user_id == user_id)
        .returning(UserPassword.c.user_id)
    )
    return (await conn.execute(stmt)).first() is not None


async def query_password_state(conn: AsyncConnection, user_id: int):
    """Whether this account has a password and when it last changed, for the
    profile page to describe a credential it can never show.

    None when there is no password, which is what the section's other half
    reads as.
    """
    stmt = select(UserPassword.c.created_at, UserPassword.c.updated_at).where(
        UserPassword.c.user_id == user_id
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

    `archived_at` here is the *effective* one - `_effective_archived_at`'s
    second caller, which is what earns it a name - so `cocco ls` marks a
    challenge whose course was archived as well as one archived on its own. A
    push to either is still accepted; see `api_import_challenge`.

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


# ---------------------------------------------------------------------------
# Archiving, and the one thing this application does that destroys a
# submission. Like every other mutation in this module, none of these commit:
# the caller owns the transaction.


async def query_course_archived_at(
    conn: AsyncConnection, course_id: int
) -> datetime | None:
    """When the course was archived, or None if it is live - or if there is no
    such course at all. `post_course_enroll` has already established that it
    exists, exactly as `query_enrollment_key` assumes.
    """
    stmt = select(Course.c.archived_at).where(Course.c.id == course_id)
    return (await conn.execute(stmt)).scalar_one_or_none()


async def query_effective_archived_at(
    conn: AsyncConnection, challenge_id: int
) -> datetime | None:
    """When this challenge became read-only: its own `archived_at`, or its
    course's.

    The single Python-side read of the rule - `post_submission` is its caller,
    and `query_challenge` answers the same question in SQL for the page. Plain
    Core, because one row is not a document.
    """
    stmt = (
        select(_effective_archived_at())
        .select_from(
            Challenge.outerjoin(Course, Course.c.id == Challenge.c.course_id)
        )
        .where(Challenge.c.id == challenge_id)
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


async def query_challenge_visibility(
    conn: AsyncConnection, challenge_id: int
) -> tuple[datetime | None, bool]:
    """When this challenge opens, and whether it has. `_challenge_visible`'s
    second caller, which is what earns it a name.

    `query_effective_archived_at`'s twin: the single Python-side read of the
    rule, plain Core for its reason, with `query_challenge` answering the same
    question in SQL for the page. Both
    halves come back in one round trip because `require_challenge_visible`
    needs both - the boolean to decide, and the timestamp to say when to come
    back.

    A challenge that does not exist answers `(None, False)`, which is what a
    hidden one answers, and is the right answer to both: the caller 404s.
    """
    stmt = select(Challenge.c.visible_from, _challenge_visible()).where(
        Challenge.c.id == challenge_id
    )
    row = (await conn.execute(stmt)).first()
    if row is None:
        return None, False
    return row[0], bool(row[1])


async def set_course_archived(
    conn: AsyncConnection, course_id: int, archived: bool
) -> None:
    """Archive or unarchive a course.

    The timestamp is stamped here rather than passed in, exactly as
    `block_user` stamps `blocked_at`: "now" belongs beside the write.
    Unarchiving clears the column rather than writing a second one - an archive
    that is over has no date, the way `unblock_user` clears the reason along
    with the block.

    Deliberately not the `update_course(**fields)` helper: that shape exists
    for a caller saving one half of a form, and this is not a form field.
    """
    await conn.execute(
        update(Course)
        .where(Course.c.id == course_id)
        .values(archived_at=func.now() if archived else None)
    )


async def set_challenge_archived(
    conn: AsyncConnection, challenge_id: int, archived: bool
) -> None:
    """`set_course_archived` for one challenge. Archiving the course it is on
    makes it read-only whatever this column says; see `_effective_archived_at`.
    """
    await conn.execute(
        update(Challenge)
        .where(Challenge.c.id == challenge_id)
        .values(archived_at=func.now() if archived else None)
    )


async def query_challenge_file_paths(
    conn: AsyncConnection, challenge_id: int
) -> list[str]:
    """Every file this challenge owns: each test case's two, and the evaluator
    when it has one of its own rather than the bundled default.

    Read *before* the rows are deleted and unlinked *after* the commit - the
    order `post_testcase_delete` states, so a failed commit can leave a file
    nothing points at but never a row pointing at a file that is gone.
    """
    paths: list[str] = []
    rows = await conn.execute(
        select(TestCase.c.input_path, TestCase.c.expected_output_path).where(
            TestCase.c.challenge_id == challenge_id
        )
    )
    for row in rows:
        paths.append(row.input_path)
        paths.append(row.expected_output_path)

    evaluator_path = (
        await conn.execute(
            select(Challenge.c.evaluator_path).where(Challenge.c.id == challenge_id)
        )
    ).scalar_one_or_none()
    if evaluator_path:
        paths.append(evaluator_path)
    return paths


async def lock_challenge_submissions(
    conn: AsyncConnection, challenge_id: int
) -> int:
    """Take every submission row of a challenge FOR UPDATE, and answer how many
    of them a judge is running.

    `count_running_submissions` asked before a *delete* would only be usually
    true, the way "a course never loses its last teacher" was before
    `lock_course`: under READ COMMITTED a judge can claim a queued submission
    between the count and the commit. `FOR UPDATE` makes the check and the
    write one statement - a claim in flight is waited for and therefore
    counted, and one that has not started yet blocks until this transaction
    commits, by which time `pull_queued_submission`'s UPDATE matches no row and
    the judge simply goes back to sleep.

    A delete needs that where a regrade does not, and the difference is sharp.
    `run_from_queue` writes its outcomes inside `engine.begin()` with no
    `except` around it, so a submission deleted mid-run makes
    `insert_test_outcomes` violate a foreign key and takes the judge *process*
    down - where the same race against a regrade only leaves one stale score.

    The locks are taken on `submission` before anything touches `testoutcome`,
    which is the order the judge takes them in, so the two cannot deadlock.
    """
    stmt = (
        select(Submission.c.status)
        .where(Submission.c.challenge_id == challenge_id)
        .with_for_update()
    )
    rows = (await conn.execute(stmt)).scalars().all()
    return sum(1 for status in rows if status == SubmissionStatus.running)


async def lock_course_submissions(conn: AsyncConnection, course_id: int) -> int:
    """`lock_challenge_submissions` over every challenge of a course.

    A subquery on `challenge` rather than a join, so the `FOR UPDATE` needs no
    `OF`: it locks `submission` and only `submission`, which is what keeps the
    lock order the same as the per-challenge version's.
    """
    stmt = (
        select(Submission.c.status)
        .where(
            Submission.c.challenge_id.in_(
                select(Challenge.c.id).where(Challenge.c.course_id == course_id)
            )
        )
        .with_for_update()
    )
    rows = (await conn.execute(stmt)).scalars().all()
    return sum(1 for status in rows if status == SubmissionStatus.running)


async def delete_challenge(conn: AsyncConnection, challenge_id: int) -> None:
    """Delete a challenge and everything that hangs off it.

    Hand-ordered, because no foreign key in `cocco/model.py` declares an
    `ON DELETE` - and that is deliberate rather than an omission:
    `testoutcome.testcase_id` having none is exactly what makes it the backstop
    stopping `delete_testcase` from stranding a score. So the order is written
    out here, once, and the database's job is to raise if it is ever got wrong.

    Both outcome deletes, and not one: `testoutcome.submission_id` and
    `testcase_id` are both nullable, so an outcome detached from its submission
    could still be the only thing holding a test case down.

    This is the one place in the application that destroys a submission.
    Everything else draws the opposite line - a block stops a person without
    retracting their work, `delete_enrollment` takes one row and nothing else,
    a test case that has graded something is retired and never deleted. That
    line is about a course that still exists; there is nothing left for a score
    to be the record *of* once the challenge is gone.

    The caller has taken `lock_challenge_submissions` first, and unlinks the
    files afterwards - `query_challenge_file_paths` says why in that order.
    """
    submission_ids = select(Submission.c.id).where(
        Submission.c.challenge_id == challenge_id
    )
    testcase_ids = select(TestCase.c.id).where(
        TestCase.c.challenge_id == challenge_id
    )
    await conn.execute(
        delete(TestOutcome).where(TestOutcome.c.submission_id.in_(submission_ids))
    )
    await conn.execute(
        delete(TestOutcome).where(TestOutcome.c.testcase_id.in_(testcase_ids))
    )
    await conn.execute(
        delete(Submission).where(Submission.c.challenge_id == challenge_id)
    )
    await conn.execute(delete(TestCase).where(TestCase.c.challenge_id == challenge_id))
    await conn.execute(delete(Challenge).where(Challenge.c.id == challenge_id))


async def delete_course(conn: AsyncConnection, course_id: int) -> None:
    """Delete a course, every challenge on it and every enrollment.

    `delete_challenge` for each one rather than a second cascade written wider:
    the order is a rule, and a rule spelled twice is a rule that drifts.
    `query_ordered_challenge_ids` is the list, so this and the route that has
    to collect the files before it runs walk exactly the same set.

    Nothing else in the schema references `course`: `administrator` is keyed on
    an address and has no foreign key here at all.
    """
    for challenge_id in await query_ordered_challenge_ids(conn, course_id):
        await delete_challenge(conn, challenge_id)
    await conn.execute(delete(Enrollment).where(Enrollment.c.course_id == course_id))
    await conn.execute(delete(Course).where(Course.c.id == course_id))


async def query_courses(
    conn: AsyncConnection, user_id: int, q: str = "", include_archived: bool = False
):
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

    `include_archived` is how a site administrator asks for all of them. They
    are the only people who can unarchive a course, so they have to be able to
    find one they are not enrolled on; everybody else is shown an archived
    course only in the half they are already on.
    """
    query = text("""
    WITH
    courses as (
        SELECT
            c.id,
            c.title,
            c.description,
            c.enrollment_key IS NOT NULL AS requires_key,
            c.archived_at,
            e.role
        FROM course c
        LEFT JOIN (SELECT * FROM enrollment WHERE user_id = :user_id) e
        ON c.id = e.course_id
        WHERE :q = '' OR position(lower(:q) in lower(c.title)) > 0
    ),
    -- coalesce: json_agg over no rows is NULL, and a user enrolled in every
    -- course (or none) would otherwise hand the template a null to iterate
    enrolled_courses AS (
        -- The archived ones stay here, badged: the people already on a course
        -- go on reading it. It is only the offer that is withdrawn.
        SELECT coalesce(json_agg(courses ORDER BY courses.title), '[]'::json) AS enrolled
        FROM courses WHERE role IS NOT null
    ),
    non_enrolled_courses AS (
        -- An archived course is not offered to somebody who is not on it: it
        -- cannot be joined, so listing it would be an invitation to a shut
        -- door.
        SELECT coalesce(json_agg(courses ORDER BY courses.title), '[]'::json) AS non_enrolled
        FROM courses
        WHERE role IS null AND (:include_archived OR archived_at IS null)
    ),
    u AS (
        SELECT * from enrolled_courses, non_enrolled_courses
    )
    SELECT row_to_json(u) FROM u;
    """)
    res = await conn.execute(
        query, dict(user_id=user_id, q=q, include_archived=include_archived)
    )
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

    `archived_at` rides along because the enroll preview is one of the two
    places that has to say the course is not taking new students - the other
    being `post_course_enroll`, which refuses.
    """
    stmt = select(
        Course.c.id,
        Course.c.title,
        Course.c.description,
        (Course.c.enrollment_key.is_not(None)).label("requires_key"),
        Course.c.archived_at,
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
#
# It carries `number` rather than leaving the template to count with
# `loop.index`, and that is load-bearing now that the list can be filtered: a
# student is shown neither the hidden challenges nor - in the editor's copy of
# this list - anything staff have not published, so counting rows on the page
# would print "Challenge 03" to a student in front of what staff call 04. The
# window runs over every challenge of the course, inside the CTE, *before* any
# caller filters it. It is the same rank `query_challenge` computes.
_COURSE_CHALLENGES_CTE = f"""
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
               -- The *effective* one, so a card under an archived course greys
               -- itself without the template having to know that a course can
               -- archive a challenge. `challenge.archived_at` rides along in
               -- the star and is the challenge's own state; only the challenge
               -- editor's danger zone reads that one. A separate name and
               -- never `AS archived_at`: that would put two columns of the
               -- same name in the record, `row_to_json` would emit both keys,
               -- and `json.loads` would resolve it last-one-wins.
               {_EFFECTIVE_ARCHIVED_SQL} AS effective_archived_at,
               -- Whether the class may see it at all, as against whether they
               -- may still submit to it. A separate name for the same reason
               -- as above, and `visible_from` itself rides along in the star:
               -- a scheduled challenge's card has to print *when*.
               {_CHALLENGE_VISIBLE_SQL} AS is_visible,
               row_number() OVER (ORDER BY challenge.position, challenge.id)
                   AS number,
               coalesce(submitted.n_mine, 0) AS n_mine,
               coalesce(submitted.n_submitters, 0) AS n_submitters
        FROM challenge
        LEFT JOIN course ON course.id = challenge.course_id
        LEFT JOIN submitted ON submitted.challenge_id = challenge.id
        WHERE challenge.course_id = :course_id
    )
"""


# Which of the CTE's rows this caller is allowed to be shown. Staff pass
# `include_hidden` and get the lot, because that is the list they publish from;
# everybody else gets the visible challenges *and* the scheduled ones, whose
# card is a teaser with no link - so the class can see what is coming - and
# never a hidden one, which they are not told exists.
#
# Applied outside the CTE rather than in its WHERE, so that `number` is already
# ranked over the whole course by the time anything is dropped.
_VISIBLE_TO_CALLER_SQL = "(:include_hidden OR visible_from IS NOT NULL)"


async def query_course_info(
    conn: AsyncConnection, course_id: int, user_id: int, include_hidden: bool = False
):
    """The course, its description and its challenges as the page lists them.

    One document rather than a query per card: see `_COURSE_CHALLENGES_CTE` for
    what each challenge carries. The course editor reads the same thing - it
    shows the same list, plus the arrows - so every reorder re-renders from
    here and cannot drift from what is stored.

    `include_hidden` is what makes the two lists differ, and it defaults to the
    safe answer: a caller who forgets it shows a student's list, never a
    teacher's. See `_VISIBLE_TO_CALLER_SQL`.
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
                 FROM challenges WHERE {_VISIBLE_TO_CALLER_SQL}),
                '[]'::json
            ) as challenges
        FROM course
        WHERE course.id = :course_id
    )
    SELECT row_to_json(c) FROM c;
    """)
    res = await conn.execute(
        query,
        dict(course_id=course_id, user_id=user_id, include_hidden=include_hidden),
    )
    return res.scalar()


async def query_course_challenges(
    conn: AsyncConnection, course_id: int, user_id: int, include_hidden: bool = False
):
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
    FROM challenges WHERE {_VISIBLE_TO_CALLER_SQL};
    """)
    res = await conn.execute(
        query,
        dict(course_id=course_id, user_id=user_id, include_hidden=include_hidden),
    )
    return res.scalar()

