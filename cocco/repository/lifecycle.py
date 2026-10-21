from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    ExerciseJob,
    ExerciseSession,
    ExerciseSessionParticipant,
    ExerciseSessionReveal,
    ExerciseSet,
    ExerciseSetVersion,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
)
from cocco.repository.courses import query_ordered_challenge_ids
from cocco.repository.rules import _challenge_visible, _effective_archived_at


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


async def delete_exercise_session(conn: AsyncConnection, session_id: int) -> None:
    """Delete a session: its jobs, its reveals, its participants, then the
    session. Formative work with nothing to keep a record of, unlike a
    challenge's submissions. Does not commit.

    The caller has taken the session's row `FOR UPDATE`
    (`query_exercise_session_state(lock="update")`): a job being queued holds
    it `FOR SHARE`, and a participant or a reveal being written holds it
    `FOR KEY SHARE` through its foreign key, so none of them can land between
    these deletes and the last one. A judge finishing one of its jobs
    meanwhile updates zero rows (`finish_exercise_job`), so nothing waits for
    the judge.
    """
    await conn.execute(delete(ExerciseJob).where(ExerciseJob.c.session_id == session_id))
    await conn.execute(
        delete(ExerciseSessionReveal).where(ExerciseSessionReveal.c.session_id == session_id)
    )
    await conn.execute(
        delete(ExerciseSessionParticipant).where(
            ExerciseSessionParticipant.c.session_id == session_id
        )
    )
    await conn.execute(delete(ExerciseSession).where(ExerciseSession.c.id == session_id))


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

    The single-row read of `_effective_archived_at` - `post_submission` is its
    caller, and `query_challenge` answers the same question for the page.
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
    """When this challenge opens, and whether it has (`_challenge_visible`).

    `query_effective_archived_at`'s twin: the single-row read of the rule, with
    `query_challenge` answering the same question for the page. Both halves
    come back in one round trip because `require_challenge_visible`
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


async def lock_course_exercises(conn: AsyncConnection, course_id: int) -> None:
    """Lock a course's exercise sessions, versions and sets for its delete.
    Does not commit.

    `lock_exercise_set` widened to a course, with the sessions in front - the
    order every writer takes them in, so a delete queues up behind each of
    them instead of deadlocking: a job being queued holds its session, then
    its version through the foreign key; a finishing build writes its version,
    then its set. Holding the sessions keeps out new jobs, participants and
    reveals, the versions new previews, and the sets new versions.

    `lock_exercise_set`'s window stays open, wider: a set or a session created
    between these locks and the deletes is not covered by them. The deletes
    still take it, since each statement sees what has committed, and in the
    rare case it brings a row the deletes did not expect the course's foreign
    keys refuse and the whole delete rolls back - nothing is left half-done.
    No lock waits for the judge: a job it finishes on a deleted session or
    version updates zero rows (`finish_exercise_job`).
    """
    set_ids = select(ExerciseSet.c.id).where(ExerciseSet.c.course_id == course_id)
    await conn.execute(
        select(ExerciseSession.c.id)
        .where(ExerciseSession.c.set_id.in_(set_ids))
        .with_for_update()
    )
    await conn.execute(
        select(ExerciseSetVersion.c.id)
        .where(ExerciseSetVersion.c.set_id.in_(set_ids))
        .with_for_update()
    )
    await conn.execute(
        select(ExerciseSet.c.id).where(ExerciseSet.c.course_id == course_id).with_for_update()
    )


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

    The exercise sets go the same way: `delete_exercise_session` for each of
    their sessions, then `delete_exercise_set`, which is the plan's order -
    jobs, reveals, participants, sessions, then the versions and the set. The
    caller has taken `lock_course_exercises`.

    Nothing else in the schema references `course`: `administrator` is keyed on
    an address and has no foreign key here at all.
    """
    for challenge_id in await query_ordered_challenge_ids(conn, course_id):
        await delete_challenge(conn, challenge_id)
    set_ids = (
        await conn.execute(
            select(ExerciseSet.c.id).where(ExerciseSet.c.course_id == course_id)
        )
    ).scalars().all()
    for set_id in set_ids:
        session_ids = (
            await conn.execute(
                select(ExerciseSession.c.id).where(ExerciseSession.c.set_id == set_id)
            )
        ).scalars().all()
        for session_id in session_ids:
            await delete_exercise_session(conn, session_id)
        await delete_exercise_set(conn, set_id)
    await conn.execute(delete(Enrollment).where(Enrollment.c.course_id == course_id))
    await conn.execute(delete(Course).where(Course.c.id == course_id))
