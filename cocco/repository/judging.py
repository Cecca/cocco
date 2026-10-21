from sqlalchemy import (
    Connection,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.schema import SubmissionRunData
from cocco.model import (
    Challenge,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSet,
    ExerciseSetVersion,
    ExerciseVersionStatus,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
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
# A session's checks are never pruned - progress is derived from them.
RUN_JOB_RETENTION = "6 months"


def prune_run_jobs(conn: Connection, limit: int) -> int:
    """Delete up to `limit` finished jobs older than `RUN_JOB_RETENTION` that
    count towards nothing, and say how many. The judge calls it, in batches,
    when it is idle.

    That is every run, and every staff preview's check: a preview has no
    session (`session_id` NULL), so no progress query ever reads one. A build
    is kept, being what its version's problems were found by."""
    result = conn.execute(
        delete(ExerciseJob).where(
            ExerciseJob.c.id.in_(
                select(ExerciseJob.c.id)
                .where(
                    ExerciseJob.c.status == ExerciseJobStatus.done,
                    or_(
                        ExerciseJob.c.kind == ExerciseJobKind.run,
                        and_(
                            ExerciseJob.c.kind == ExerciseJobKind.check,
                            ExerciseJob.c.session_id.is_(None),
                        ),
                    ),
                    ExerciseJob.c.enqueued
                    < func.now() - text(f"interval '{RUN_JOB_RETENTION}'"),
                )
                .limit(limit)
            )
        )
    )
    return result.rowcount
