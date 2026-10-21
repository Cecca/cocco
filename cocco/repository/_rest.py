from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    Boolean,
    and_,
    bindparam,
    case,
    column,
    delete,
    exists,
    false,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import (
    JSONB,
    aggregate_order_by,
    insert as pg_insert,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.model import (
    Challenge,
    Course,
    Enrollment,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSession,
    ExerciseSessionMode,
    ExerciseSessionParticipant,
    ExerciseSessionReveal,
    ExerciseSet,
    ExerciseSetVersion,
    ExerciseVersionStatus,
    Submission,
    SubmissionStatus,
    TestCase,
    TestOutcome,
    User,
    UserRole,
)
from cocco.repository.courses import query_ordered_challenge_ids
from cocco.repository.judging import notify_queue
from cocco.repository.rules import (
    _EMPTY_JSON_ARRAY,
    _challenge_visible,
    _effective_archived_at,
    _json_object,
)


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
        # `submitted_at` by hand: an upsert's DO UPDATE is not an UPDATE to
        # SQLAlchemy, and the column has no `onupdate` anyway - the judge's
        # writes to the row are not the student's.
        set_={
            "source": stmt.excluded.source,
            "stdin": stmt.excluded.stdin,
            "submitted_at": func.now(),
        },
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


def _version_number(version):
    """A version's `number` within its set, oldest first, so "v3" means the
    third import of *this* set whatever the ids are. A window over `version`,
    a table or alias of `exercise_set_version`: select it over every version
    of the set, before picking any of them out."""
    return func.row_number().over(partition_by=version.c.set_id, order_by=version.c.id)


def _count_problems(version, level: str):
    """How many of a version's build problems are at `level`, as a scalar
    subquery correlated to `version`."""
    problem = func.jsonb_array_elements(version.c.problems).table_valued(
        column("value", JSONB), name="p"
    )
    return (
        select(func.count())
        .select_from(problem)
        .where(problem.c.value["level"].astext == level)
        .scalar_subquery()
    )


def _exercise_versions(condition, *, counts: bool = True):
    """The versions of every set matching `condition` - a Core boolean over
    `exercise_set` and `exercise_set_version` - as a CTE named `versions`:
    each version's status and `number` (`_version_number`) and, with `counts`,
    what the set page shows about it - how many problems of each level it has
    and, once built, how many exercises.

    The problems themselves are not listed, only counted: the set page prints
    the latest version's in full and nobody else's, and the fragment carrying
    this is polled. A listing that needs only numbers and statuses passes
    `counts=False` rather than count every version's problems - for an
    administrator, every version on the site.
    """
    columns = [
        ExerciseSetVersion.c.id,
        ExerciseSetVersion.c.set_id,
        ExerciseSetVersion.c.title,
        ExerciseSetVersion.c.status,
        ExerciseSetVersion.c.created_at,
        _version_number(ExerciseSetVersion).label("number"),
    ]
    if counts:
        columns += [
            _n_exercises(ExerciseSetVersion).label("n_exercises"),
            _count_problems(ExerciseSetVersion, "error").label("n_errors"),
            _count_problems(ExerciseSetVersion, "warning").label("n_warnings"),
        ]
    return (
        select(*columns)
        .join(ExerciseSet, ExerciseSet.c.id == ExerciseSetVersion.c.set_id)
        .where(condition)
        .cte("versions")
    )


def _latest_version(versions):
    """The newest row of `versions` for each `exercise_set` in the enclosing
    FROM, as a LATERAL subquery named `latest` - to be joined `ON true`.

    Correlated to `exercise_set` explicitly, to say what it reads from the
    enclosing statement. SQLAlchemy does not enforce that: in a statement
    without `exercise_set` in its FROM it would put the table in the
    subquery's own FROM and read the newest version on the whole site."""
    return (
        select(versions)
        .where(versions.c.set_id == ExerciseSet.c.id)
        .order_by(versions.c.id.desc())
        .limit(1)
        .correlate(ExerciseSet)
        .lateral("latest")
    )


def _exercise_set_title(current, latest):
    """A set's title, which is its current version's: a failed re-import must
    not rename the set it failed to change, on the course page or in the delete
    dialog. The latest version's only while there is no current one - a set
    whose first build has not succeeded yet still needs a name. Takes the
    `current` and `latest` rows of `_exercise_versions`."""
    return func.coalesce(current.c.title, latest.c.title)


async def query_exercise_sets(conn: AsyncConnection, course_id: int):
    """The exercise sets of a course, as its page lists them for staff.

    The title and the exercise count are the current version's - see
    `_exercise_set_title` - and the status badge is the latest's, which is
    what tells its author a re-import is building or broke.
    """
    course = bindparam("course_id", course_id)
    versions = _exercise_versions(ExerciseSet.c.course_id == course)
    latest = _latest_version(versions)
    current = versions.alias("current")
    sets = (
        select(
            ExerciseSet.c.id,
            ExerciseSet.c.archived_at,
            ExerciseSet.c.current_version_id,
            _exercise_set_title(current, latest).label("title"),
            latest.c.status.label("latest_status"),
            latest.c.number.label("latest_number"),
            current.c.number.label("current_number"),
            current.c.n_exercises,
        )
        .select_from(
            ExerciseSet.join(latest, true()).outerjoin(
                current, current.c.id == ExerciseSet.c.current_version_id
            )
        )
        .where(ExerciseSet.c.course_id == course)
        .cte("sets")
    )
    stmt = select(
        func.coalesce(
            func.json_agg(aggregate_order_by(sets.table_valued(), sets.c.id)),
            _EMPTY_JSON_ARRAY,
        )
    )
    return (await conn.execute(stmt)).scalar()


async def query_exercise_set(conn: AsyncConnection, set_id: int):
    """The set page's document: the set and its title, every version of it
    newest first, the latest version's problems in full, whether any version
    is still building, and how many sessions it has.

    No version's source or document: the page that polls this shows statuses,
    and the preview reads a document through `query_exercise_version`.
    """
    exercise_set = bindparam("set_id", set_id)
    versions = _exercise_versions(ExerciseSetVersion.c.set_id == exercise_set)
    latest = (
        select(versions, ExerciseSetVersion.c.problems)
        .join(ExerciseSetVersion, ExerciseSetVersion.c.id == versions.c.id)
        .order_by(versions.c.id.desc())
        .limit(1)
        .cte("latest")
    )
    current = versions.alias("current")
    every_version = select(
        func.json_agg(aggregate_order_by(versions.table_valued(), versions.c.id.desc()))
    ).scalar_subquery()
    n_sessions = (
        select(func.count())
        .where(ExerciseSession.c.set_id == ExerciseSet.c.id)
        .scalar_subquery()
    )
    s = (
        select(
            ExerciseSet,
            _exercise_set_title(current, latest).label("title"),
            func.row_to_json(latest.table_valued()).label("latest"),
            # Any, not only the latest: two imports in quick succession are
            # two builds, and the older one finishing is news too.
            exists()
            .where(versions.c.status == ExerciseVersionStatus.building)
            .label("any_building"),
            func.coalesce(every_version, _EMPTY_JSON_ARRAY).label("versions"),
            n_sessions.label("n_sessions"),
        )
        .select_from(
            ExerciseSet.join(latest, true()).outerjoin(
                current, current.c.id == ExerciseSet.c.current_version_id
            )
        )
        .where(ExerciseSet.c.id == exercise_set)
        .cte("s")
    )
    stmt = select(func.row_to_json(s.table_valued()))
    return (await conn.execute(stmt)).scalar()


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
    exercise_set = bindparam("set_id", set_id)
    # Numbered over every version of the set, and only the ids carried through
    # the window: the one source wanted is joined on afterwards.
    numbers = (
        select(
            ExerciseSetVersion.c.id,
            _version_number(ExerciseSetVersion).label("number"),
        )
        .where(ExerciseSetVersion.c.set_id == exercise_set)
        .subquery("numbers")
    )
    stmt = (
        select(
            ExerciseSetVersion.c.source,
            ExerciseSetVersion.c.title,
            numbers.c.number,
        )
        .join(numbers, numbers.c.id == ExerciseSetVersion.c.id)
        .where(ExerciseSetVersion.c.id == version_id)
    )
    return (await conn.execute(stmt)).first()


async def query_exercise_set_course(conn: AsyncConnection, set_id: int) -> int | None:
    """The course a set belongs to, or None - what the transfer API resolves
    the caller's role from, since its URL carries only the set."""
    stmt = select(ExerciseSet.c.course_id).where(ExerciseSet.c.id == set_id)
    return (await conn.execute(stmt)).scalar_one_or_none()


async def query_exercise_version_summary(
    conn: AsyncConnection,
    set_id: int,
    version_id: int | None = None,
    *,
    with_source: bool = False,
):
    """One version of a set - the newest, whatever its status, when
    `version_id` is None - as the transfer API reports it: its title, status,
    problems, `number` within the set and whether it is the current one, and
    its source only when asked: a push's check and the poll that waits for its
    build have no use for the file.

    The newest rather than the current, because that is what a push has to be
    conditional on: a re-import that is still building, or that failed, is
    still somebody's change, and a pull that handed out the current version
    instead would quietly take a failed file's fixes away from its author.
    """
    exercise_set = bindparam("set_id", set_id)
    numbers = (
        select(
            ExerciseSetVersion.c.id,
            _version_number(ExerciseSetVersion).label("number"),
        )
        .where(ExerciseSetVersion.c.set_id == exercise_set)
        .subquery("numbers")
    )
    columns = [
        ExerciseSetVersion.c.id,
        ExerciseSetVersion.c.title,
        ExerciseSetVersion.c.status,
        ExerciseSetVersion.c.problems,
        numbers.c.number,
        (ExerciseSet.c.current_version_id == ExerciseSetVersion.c.id)
        .is_(True)
        .label("is_current"),
    ]
    if with_source:
        columns.append(ExerciseSetVersion.c.source)
    stmt = select(*columns).select_from(
        ExerciseSetVersion.join(numbers, numbers.c.id == ExerciseSetVersion.c.id).join(
            ExerciseSet, ExerciseSet.c.id == ExerciseSetVersion.c.set_id
        )
    )
    if version_id is None:
        stmt = stmt.order_by(ExerciseSetVersion.c.id.desc()).limit(1)
    else:
        stmt = stmt.where(ExerciseSetVersion.c.id == version_id)
    return (await conn.execute(stmt)).first()


async def query_editable_exercise_sets(
    conn: AsyncConnection,
    user_id: int,
    editor_roles: Iterable[UserRole],
    every_course: bool = False,
):
    """`query_editable_challenges` for exercise sets: every set the caller may
    pull and push, with its course, its title (`_exercise_set_title`) and
    its newest version's number and status, which is what a push would be
    conditional on.

    `archived` is the set's or its course's, a note beside the title as it is
    for a challenge and never a gate.
    """
    editable = (
        true()
        if every_course
        else ExerciseSet.c.course_id.in_(
            select(Enrollment.c.course_id).where(
                Enrollment.c.user_id == user_id,
                Enrollment.c.role.in_(list(editor_roles)),
            )
        )
    )
    versions = _exercise_versions(editable, counts=False)
    latest = _latest_version(versions)
    current = versions.alias("current")
    stmt = (
        select(
            Course.c.id.label("course_id"),
            Course.c.title.label("course_title"),
            ExerciseSet.c.id.label("set_id"),
            _exercise_set_title(current, latest).label("title"),
            or_(
                ExerciseSet.c.archived_at.is_not(None),
                Course.c.archived_at.is_not(None),
            ).label("archived"),
            latest.c.number.label("latest_number"),
            latest.c.status.label("latest_status"),
            current.c.number.label("current_number"),
        )
        .select_from(
            ExerciseSet.join(Course, Course.c.id == ExerciseSet.c.course_id)
            .join(latest, true())
            .outerjoin(current, current.c.id == ExerciseSet.c.current_version_id)
        )
        .where(editable)
        .order_by(Course.c.title, Course.c.id, ExerciseSet.c.id)
    )
    return (await conn.execute(stmt)).mappings().all()


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


async def lock_exercise_set_for_push(conn: AsyncConnection, set_id: int) -> bool:
    """Serialize pushes to a set, so that two of them based on the same
    version cannot both pass `If-Match` and both import. False if there is no
    such set. Does not commit: the lock is held until the import commits.

    `FOR NO KEY UPDATE`, which conflicts with itself but not with the `KEY
    SHARE` a foreign key takes, so a web import's version insert or a preview
    job is not held up by it. Only the set's row: an import inserts a version
    and touches no existing one, so it cannot close a cycle with a delete
    (versions, then the set) or a finishing build (its version, then the set).
    """
    stmt = (
        select(ExerciseSet.c.id)
        .where(ExerciseSet.c.id == set_id)
        .with_for_update(key_share=True)
    )
    return (await conn.execute(stmt)).first() is not None


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


# --- Exercise sessions ---------------------------------------------------------
#
# A session pins a version of a set and carries the pacing; see the plan's
# "Sessions carry pacing and progress". Every write below is a single
# conditional statement, the way `block_user` refuses an administrator: the
# rule ("only while open", "only from where the page said it was") is in the
# `WHERE` clause, so two teachers pressing the same button at once cannot both
# act on what neither of them saw.


# How many exercises a version has. Its document is NULL until it is built, and
# a session is only ever on a ready one.
def _n_exercises(version):
    return func.jsonb_array_length(version.c.document["exercises"])


def _session_n_exercises():
    """The exercise count of the version an `exercise_session` row being
    updated is on, as a correlated subquery for its `SET`."""
    return (
        select(_n_exercises(ExerciseSetVersion))
        .where(ExerciseSetVersion.c.id == ExerciseSession.c.version_id)
        .scalar_subquery()
    )


async def exercise_session_in_course(
    conn: AsyncConnection, session_id: int, course_id: int
) -> bool:
    """`exercise_set_in_course` for a session, through its set: the role
    comes from the course in the URL, so the session in it is checked against
    that course rather than trusted."""
    stmt = (
        select(ExerciseSession.c.id)
        .join(ExerciseSet, ExerciseSet.c.id == ExerciseSession.c.set_id)
        .where(ExerciseSession.c.id == session_id, ExerciseSet.c.course_id == course_id)
    )
    return (await conn.execute(stmt)).first() is not None


@dataclass(frozen=True)
class ExerciseSetForSession:
    """What `lock_exercise_set_for_session` read under its lock."""

    current_version_id: int | None
    archived_at: datetime | None
    # The current version's; None exactly when there is no current version.
    n_exercises: int | None


async def lock_exercise_set_for_session(
    conn: AsyncConnection, set_id: int
) -> ExerciseSetForSession | None:
    """Lock a set against deletion while a session is opened on it, and read
    what the session is opened on: the set's `current_version_id`,
    `archived_at` and the current version's exercise count. None if the set
    does not exist. Does not commit.

    `lock_exercise_set`'s counterpart, and in its order - the versions, then
    the set - since the other order deadlocks against it: a delete holding the
    versions and waiting for the set, an opening holding the set and waiting
    for the version its new row's foreign key names. The versions are taken
    `FOR KEY SHARE`, which a delete's `FOR UPDATE` waits for and a finishing
    build's `UPDATE` does not; the set `FOR SHARE`, which waits for that
    build to commit, so the current version read here is the one it left.

    The set is locked on its own and the count read in a second statement,
    never joined into the locking one: a locking read that waited for a
    concurrent `UPDATE` re-checks its join against the joined row *as it was*,
    so a build moving `current_version_id` meanwhile would come back with the
    new id and a NULL count - or, on the first build, no current version at all.
    """
    await conn.execute(
        select(ExerciseSetVersion.c.id)
        .where(ExerciseSetVersion.c.set_id == set_id)
        .with_for_update(key_share=True)
    )
    row = (
        await conn.execute(
            select(ExerciseSet.c.current_version_id, ExerciseSet.c.archived_at)
            .where(ExerciseSet.c.id == set_id)
            .with_for_update(read=True)
        )
    ).first()
    if row is None:
        return None
    n_exercises = (
        None
        if row.current_version_id is None
        else await query_version_exercise_count(conn, row.current_version_id)
    )
    return ExerciseSetForSession(row.current_version_id, row.archived_at, n_exercises)


async def query_version_exercise_count(conn: AsyncConnection, version_id: int) -> int | None:
    """How many exercises a version has; None while it has no document."""
    stmt = select(_n_exercises(ExerciseSetVersion)).where(
        ExerciseSetVersion.c.id == version_id
    )
    return (await conn.execute(stmt)).scalar_one_or_none()


async def insert_exercise_session(
    conn: AsyncConnection,
    set_id: int,
    version_id: int,
    label: str,
    mode: ExerciseSessionMode,
    unlocked_through: int,
) -> int:
    """A session on `version_id`, which the caller read under
    `lock_exercise_set_for_session`. Does not commit."""
    stmt = (
        insert(ExerciseSession)
        .values(
            set_id=set_id,
            version_id=version_id,
            label=label,
            mode=mode,
            unlocked_through=unlocked_through,
        )
        .returning(ExerciseSession.c.id)
    )
    return (await conn.execute(stmt)).scalar_one()


@dataclass(frozen=True)
class ExerciseSessionState:
    """What `query_exercise_session_state` answers."""

    id: int
    set_id: int
    version_id: int
    mode: ExerciseSessionMode
    unlocked_through: int
    closed_at: datetime | None
    n_exercises: int


async def query_exercise_session_state(
    conn: AsyncConnection, session_id: int, lock: str | None = None
) -> ExerciseSessionState | None:
    """What pacing is decided on: the session's mode, `unlocked_through`,
    `closed_at`, its version and that version's exercise count - or None.

    `lock` is None for a plain read, `"share"` for the transaction that queues
    a job, and `"update"` for the one that switches the version: the two
    conflict, so a check cannot be queued on an exercise the switch has just
    counted as absent, and a close or a pacing move - each an `UPDATE` of the
    row - waits for a job being queued to commit, and the other way round.

    The row is locked on its own and the version's count read in a second
    statement, for `lock_exercise_set_for_session`'s reason: joined into the
    locking read, a gate that waited behind a version switch would re-check
    the join against the version the session *was* on, find no match, and
    answer None - a 404 for a perfectly good Run.
    """
    stmt = select(
        ExerciseSession.c.id,
        ExerciseSession.c.set_id,
        ExerciseSession.c.version_id,
        ExerciseSession.c.mode,
        ExerciseSession.c.unlocked_through,
        ExerciseSession.c.closed_at,
    ).where(ExerciseSession.c.id == session_id)
    if lock is not None:
        stmt = stmt.with_for_update(read=lock == "share")
    row = (await conn.execute(stmt)).first()
    if row is None:
        return None
    n_exercises = await query_version_exercise_count(conn, row.version_id)
    return ExerciseSessionState(**row._mapping, n_exercises=n_exercises or 0)


def _exercise_titles(document):
    """A version's exercise titles, as a JSON array in order: a scalar
    subquery over `document`, a version's `document` column. A version that
    is not built has none."""
    exercise = (
        func.jsonb_array_elements(document["exercises"])
        .table_valued(column("value", JSONB), with_ordinality="i")
        .render_derived(name="x")
    )
    titles = select(
        func.json_agg(
            aggregate_order_by(exercise.c.value["title"].astext, exercise.c.i)
        )
    ).scalar_subquery()
    return func.coalesce(titles, _EMPTY_JSON_ARRAY)


async def query_exercise_session(conn: AsyncConnection, session_id: int):
    """The session's staff page: the session, its set, the version it is on
    and the set's current one - each with its `number` within the set and its
    exercises' titles - the exercises whose solution is revealed, and the
    highest exercise anybody has checked in it.

    The two versions' titles are what the version switch's confirmation is
    worked out from, and `max_checked` is what it refuses on; the route
    decides both again under the lock before it writes.
    """
    session = bindparam("session_id", session_id)
    session_set = (
        select(ExerciseSession.c.set_id)
        .where(ExerciseSession.c.id == session)
        .scalar_subquery()
    )
    versions = (
        select(
            ExerciseSetVersion.c.id,
            ExerciseSetVersion.c.title,
            ExerciseSetVersion.c.status,
            _version_number(ExerciseSetVersion).label("number"),
            _exercise_titles(ExerciseSetVersion.c.document).label("titles"),
        )
        .where(ExerciseSetVersion.c.set_id == session_set)
        .cte("versions")
    )

    def version(version_id):
        return (
            select(func.row_to_json(versions.table_valued()))
            .where(versions.c.id == version_id)
            .scalar_subquery()
        )

    max_checked = (
        select(func.max(ExerciseJob.c.exercise_index))
        .where(ExerciseJob.c.session_id == session, _is_check())
        .scalar_subquery()
    )
    doc = (
        select(
            ExerciseSession.c.id,
            ExerciseSession.c.set_id,
            ExerciseSession.c.label,
            ExerciseSession.c.mode,
            ExerciseSession.c.unlocked_through,
            ExerciseSession.c.opened_at,
            ExerciseSession.c.closed_at,
            ExerciseSet.c.course_id,
            # The set's name, which is its current version's - a session
            # exists only on a ready version, so there always is one.
            select(versions.c.title)
            .where(versions.c.id == ExerciseSet.c.current_version_id)
            .scalar_subquery()
            .label("set_title"),
            version(ExerciseSession.c.version_id).label("version"),
            version(ExerciseSet.c.current_version_id).label("latest"),
            _session_revealed(session).label("revealed"),
            max_checked.label("max_checked"),
        )
        .select_from(
            ExerciseSession.join(
                ExerciseSet, ExerciseSet.c.id == ExerciseSession.c.set_id
            )
        )
        .where(ExerciseSession.c.id == session)
        .cte("doc")
    )
    stmt = select(func.row_to_json(doc.table_valued()))
    return (await conn.execute(stmt)).scalar()


async def query_exercise_sessions(
    conn: AsyncConnection, *, course_id: int | None = None, set_id: int | None = None
):
    """The sessions of a course, or of one set, newest first: each with the
    title of the version it is on - which is what its class is working
    through, whatever the set has been re-imported as since - and that
    version's exercise count. Closed ones included; the page decides where
    they go.
    """
    where = "exercise_set.course_id = :course_id" if set_id is None else "s.set_id = :set_id"
    query = text(f"""
    WITH sessions AS (
        SELECT s.id, s.set_id, s.label, s.mode, s.unlocked_through,
               s.opened_at, s.closed_at,
               v.title,
               jsonb_array_length(v.document -> 'exercises') AS n_exercises
        FROM exercise_session s
        JOIN exercise_set ON exercise_set.id = s.set_id
        JOIN exercise_set_version v ON v.id = s.version_id
        WHERE {where}
    )
    SELECT coalesce(json_agg(sessions ORDER BY sessions.id DESC), '[]'::json)
    FROM sessions;
    """)
    params = dict(course_id=course_id) if set_id is None else dict(set_id=set_id)
    return (await conn.execute(query, params)).scalar()


async def pace_exercise_session(
    conn: AsyncConnection, session_id: int, from_: int, to: int
) -> bool:
    """Move a live session's `unlocked_through` from `from_` to `to`, clamped
    to `0..` the version's exercise count. Answers whether it moved. Does not
    commit.

    A compare-and-set rather than an increment: `from_` is what the teacher's
    page showed. Two teachers pressing *next* at once both mean "unlock the
    exercise after the one I can see", so the second finds the first already
    moved it and changes nothing - an increment would skip an exercise, and
    an absolute `to` from a stale page would take the class back. A closed
    session, or an open one, is not paced at all.
    """
    stmt = (
        update(ExerciseSession)
        .where(
            ExerciseSession.c.id == session_id,
            ExerciseSession.c.unlocked_through == from_,
            ExerciseSession.c.mode == ExerciseSessionMode.live,
            ExerciseSession.c.closed_at.is_(None),
        )
        .values(
            unlocked_through=func.least(func.greatest(to, 0), _session_n_exercises())
        )
        .returning(ExerciseSession.c.id)
    )
    return (await conn.execute(stmt)).first() is not None


async def open_exercise_session_to_all(conn: AsyncConnection, session_id: int) -> bool:
    """Switch a live session to `open`: everything unlocked, no pacing. One
    way only - homework after class - and never on a closed session. Answers
    whether it changed. Does not commit.

    `unlocked_through` goes to the exercise count, so that an open session's
    number never lies, wherever it is read.
    """
    stmt = (
        update(ExerciseSession)
        .where(
            ExerciseSession.c.id == session_id,
            ExerciseSession.c.mode == ExerciseSessionMode.live,
            ExerciseSession.c.closed_at.is_(None),
        )
        .values(mode=ExerciseSessionMode.open, unlocked_through=_session_n_exercises())
        .returning(ExerciseSession.c.id)
    )
    return (await conn.execute(stmt)).first() is not None


async def close_exercise_session(conn: AsyncConnection, session_id: int) -> bool:
    """Close a session: it accepts no new work from then on. Once - a second
    close keeps the first's time. Answers whether it was this call that closed
    it. Does not commit."""
    stmt = (
        update(ExerciseSession)
        .where(ExerciseSession.c.id == session_id, ExerciseSession.c.closed_at.is_(None))
        .values(closed_at=func.now())
        .returning(ExerciseSession.c.id)
    )
    return (await conn.execute(stmt)).first() is not None


async def reveal_exercise_solution(
    conn: AsyncConnection, session_id: int, exercise_index: int
) -> bool:
    """Show an exercise's solution to the session's class. Answers whether
    `exercise_index` is an exercise of the session's version; revealing one
    twice is a no-op on the key. Does not commit.

    The session row is taken `FOR SHARE` first. The reveal's foreign key holds
    only a key-share lock on it, which a version switch's `UPDATE` does not
    wait for - so without this a reveal checked against the old version could
    commit beside a switch to a shorter one, past its end. With it, the range
    check below runs after any switch has committed, on the version it left.
    """
    await conn.execute(
        select(ExerciseSession.c.id)
        .where(ExerciseSession.c.id == session_id)
        .with_for_update(read=True)
    )
    in_range = (
        select(literal(session_id), literal(exercise_index))
        .select_from(ExerciseSession)
        .join(ExerciseSetVersion, ExerciseSetVersion.c.id == ExerciseSession.c.version_id)
        .where(
            ExerciseSession.c.id == session_id,
            literal(exercise_index) >= 1,
            literal(exercise_index) <= _n_exercises(ExerciseSetVersion),
        )
    )
    stmt = (
        pg_insert(ExerciseSessionReveal)
        .from_select(["session_id", "exercise_index"], in_range)
        .on_conflict_do_nothing()
        .returning(ExerciseSessionReveal.c.session_id)
    )
    if (await conn.execute(stmt)).first() is not None:
        return True
    # Already revealed - the conflict returns nothing - or out of range.
    return (
        await conn.execute(
            select(ExerciseSessionReveal.c.session_id).where(
                ExerciseSessionReveal.c.session_id == session_id,
                ExerciseSessionReveal.c.exercise_index == exercise_index,
            )
        )
    ).first() is not None


async def query_max_checked_exercise(conn: AsyncConnection, session_id: int) -> int | None:
    """The highest exercise anybody has a check on in this session, on any
    version, or None. The version switch's refusal; read it under
    `query_exercise_session_state(..., lock="update")`, or a check can be
    queued on an exercise it has just counted as absent."""
    stmt = select(func.max(ExerciseJob.c.exercise_index)).where(
        ExerciseJob.c.session_id == session_id,
        _is_check(),
    )
    return (await conn.execute(stmt)).scalar_one()


async def switch_exercise_session_version(
    conn: AsyncConnection, session_id: int, version_id: int, n_exercises: int
) -> None:
    """Move a session to another version of its set - the only write that
    ever changes `exercise_session.version_id` - with `unlocked_through`
    clamped to its exercise count, and the reveals of exercises it does not
    have dropped - or, for an open session, set to the new count. Past checks
    keep the version they ran on. The caller holds
    the session's row lock and has refused a version with fewer exercises than
    the session has checks for. Does not commit."""
    await conn.execute(
        update(ExerciseSession)
        .where(ExerciseSession.c.id == session_id)
        .values(
            version_id=version_id,
            # An open session stays fully unlocked, appended exercises
            # included; a live one keeps its place, unless that is past the end.
            unlocked_through=case(
                (ExerciseSession.c.mode == ExerciseSessionMode.open, n_exercises),
                else_=func.least(ExerciseSession.c.unlocked_through, n_exercises),
            ),
        )
    )
    await conn.execute(
        delete(ExerciseSessionReveal).where(
            ExerciseSessionReveal.c.session_id == session_id,
            ExerciseSessionReveal.c.exercise_index > n_exercises,
        )
    )


async def query_set_current_version(conn: AsyncConnection, set_id: int) -> int | None:
    """The set's current version's id, or None."""
    stmt = select(ExerciseSet.c.current_version_id).where(ExerciseSet.c.id == set_id)
    return (await conn.execute(stmt)).scalar_one_or_none()


# --- The student's side of a session --------------------------------------------


async def insert_session_participant(
    conn: AsyncConnection, session_id: int, user_id: int
) -> None:
    """Record that a student came to a session: the dashboard's denominator.
    Once - a later visit is a no-op on the key, so `joined_at` is the first -
    and only while the session is not closed, a rule in the statement rather
    than a check before it: somebody reviewing a closed session afterwards did
    not take part in it. Does not commit."""
    still_open = select(literal(session_id), literal(user_id)).where(
        ExerciseSession.c.id == session_id, ExerciseSession.c.closed_at.is_(None)
    )
    await conn.execute(
        pg_insert(ExerciseSessionParticipant)
        .from_select(["session_id", "user_id"], still_open)
        .on_conflict_do_nothing()
    )


def _is_check():
    """`exercise_job.kind = 'check'`, with the kind written into the SQL
    rather than bound: it is the predicate of the partial index
    `ix_exercise_job_check`. A generic plan - which psycopg's prepared
    statements make possible after a few executions of a polled query - can
    only use that index when it sees the constant. Postgres would most likely
    keep re-planning with the bound value instead, so the literal is less a
    rescue than a guarantee: index use that does not depend on the planner's
    choice, and no re-planning to pay for it."""
    return ExerciseJob.c.kind == literal(
        ExerciseJobKind.check, ExerciseJob.c.kind.type, literal_execute=True
    )



def _session_checks(session_id, user_id=None):
    """The check jobs of one session, as a CTE `checks` with an aggregate row
    per person and exercise: what the strip colours each exercise by, and what
    the teacher's two screens count. Solved is sticky - any passing check, on
    any version the session has been on - and `checking` is a check not yet
    done. `passed` is read only off a finished check: an error result has no
    such key, which the coalesce makes "not passed". Through
    `ix_exercise_job_check`.

    Each row carries itself as `doc`, the `{n, solved, checking}` object
    `_exercise_status` reads, so the strip and the dashboard aggregate that
    and never spell the object out for themselves. Written once for all of
    them, so the strip a student sees and the cell the teacher sees for them
    cannot disagree; `user_id` narrows it to one person, the strip's."""
    done = ExerciseJob.c.status == ExerciseJobStatus.done
    passed = ExerciseJob.c.result["passed"].astext.cast(Boolean)
    conditions = [ExerciseJob.c.session_id == session_id, _is_check()]
    if user_id is not None:
        conditions.append(ExerciseJob.c.user_id == user_id)
    agg = (
        select(
            ExerciseJob.c.user_id,
            ExerciseJob.c.exercise_index.label("n"),
            func.bool_or(and_(done, func.coalesce(passed, false()))).label("solved"),
            func.bool_or(~done).label("checking"),
        )
        .where(*conditions)
        .group_by(ExerciseJob.c.user_id, ExerciseJob.c.exercise_index)
        .subquery("agg")
    )
    return select(
        agg,
        _json_object(n=agg.c.n, solved=agg.c.solved, checking=agg.c.checking).label(
            "doc"
        ),
    ).cte("checks")


def _session_cohort(checks, session_id):
    """A session's cohort, as a CTE `cohort` of user ids: the participants
    unioned with anyone who has a row in `checks` (`_session_checks`),
    `query_challenge_standings`'s rule - a check from somebody whose
    participant row is missing (a visit that raced a close) still counts. The
    dashboard's people and the projector's denominator."""
    participants = select(ExerciseSessionParticipant.c.user_id).where(
        ExerciseSessionParticipant.c.session_id == session_id
    )
    return participants.union(select(checks.c.user_id)).cte("cohort")


def _session_revealed(session_id):
    """The exercises whose solution is revealed in a session, as a JSON array
    in order: a scalar subquery."""
    revealed = select(
        func.json_agg(
            aggregate_order_by(
                ExerciseSessionReveal.c.exercise_index,
                ExerciseSessionReveal.c.exercise_index,
            )
        )
    ).where(ExerciseSessionReveal.c.session_id == session_id)
    return func.coalesce(revealed.scalar_subquery(), _EMPTY_JSON_ARRAY)


def _checks_of(checks, *conditions):
    """The `doc`s of the rows of `checks` matching `conditions`, as a JSON
    array in exercise order: a scalar subquery."""
    docs = select(func.json_agg(aggregate_order_by(checks.c.doc, checks.c.n))).where(
        *conditions
    )
    return func.coalesce(docs.scalar_subquery(), _EMPTY_JSON_ARRAY)


def _session_document(session, *columns):
    """`row_to_json` of one session, joined to the version it is on, with the
    pacing every session document starts with and `columns` after it."""
    doc = (
        select(
            ExerciseSession.c.mode,
            ExerciseSession.c.unlocked_through,
            ExerciseSession.c.closed_at,
            _exercise_titles(ExerciseSetVersion.c.document).label("titles"),
            *columns,
        )
        .select_from(
            ExerciseSession.join(
                ExerciseSetVersion,
                ExerciseSetVersion.c.id == ExerciseSession.c.version_id,
            )
        )
        .where(ExerciseSession.c.id == session)
        .cte("doc")
    )
    return select(func.row_to_json(doc.table_valued()))


async def query_session_heading(conn: AsyncConnection, session_id: int):
    """What the student page needs of a session beyond its pacing: the label
    and the exercises whose solution is revealed, as a set - or None.

    Not `query_exercise_session`, which is the staff page's and aggregates
    every version of the set to offer a switch; this page is loaded by every
    student in the room."""
    session = bindparam("session_id", session_id)
    stmt = select(
        ExerciseSession.c.label, _session_revealed(session).label("revealed")
    ).where(ExerciseSession.c.id == session)
    row = (await conn.execute(stmt)).first()
    if row is None:
        return None
    return {"label": row.label, "revealed": set(row.revealed)}


async def query_session_strip(conn: AsyncConnection, session_id: int, user_id: int):
    """The student page's exercise strip: the session's pacing, the titles of
    the version it is on, and this person's checks per exercise - or None.

    Polled every few seconds by every student in the room, so it is one round
    trip and reads no statement, no test and no source. Every title is in it;
    what the asker may see of them is `session_strip_view`'s to decide.
    """
    session = bindparam("session_id", session_id)
    checks = _session_checks(session, bindparam("user_id", user_id))
    stmt = _session_document(session, _checks_of(checks).label("checks"))
    return (await conn.execute(stmt)).scalar()


async def query_session_progress(conn: AsyncConnection, session_id: int):
    """The dashboard's document, in one round trip: the session's label and
    pacing, the titles of the version it is on, the exercises whose solution
    is revealed, and its cohort (`_session_cohort`) - each person with
    their checks per exercise - or None.

    Course staff do not come into it by being staff: they are never
    registered as participants, and a session refuses their jobs. Somebody
    who checked something as a student and was made staff afterwards is
    still in it, since the checks are what the cohort is made of. Statuses
    are derived from the checks by `session_progress_view`, the strip's own
    derivation; this query only gathers them. Polled every few seconds, so it
    reads no statement, no test and no source.
    """
    session = bindparam("session_id", session_id)
    checks = _session_checks(session)
    cohort = _session_cohort(checks, session)
    people = (
        select(
            User.c.id,
            User.c.nickname,
            User.c.email,
            _checks_of(checks, checks.c.user_id == User.c.id).label("checks"),
        )
        .select_from(cohort.join(User, User.c.id == cohort.c.user_id))
        .cte("people")
    )
    participants = select(
        func.json_agg(
            aggregate_order_by(
                people.table_valued(), func.lower(people.c.nickname), people.c.id
            )
        )
    ).scalar_subquery()
    stmt = _session_document(
        session,
        ExerciseSession.c.id,
        ExerciseSession.c.set_id,
        ExerciseSession.c.label,
        func.coalesce(participants, _EMPTY_JSON_ARRAY).label("participants"),
        _session_revealed(session).label("revealed"),
    )
    return (await conn.execute(stmt)).scalar()


async def query_session_counts(conn: AsyncConnection, session_id: int):
    """The projector's document: `query_session_progress` with nobody in it.
    The session's label and pacing, the titles, the reveals, the size of the
    cohort, and per exercise how many people have checked it, how many of
    those solved it and whether a check is pending - or None.

    Counts and not people, so the screen a class sees is never handed a
    name to show - the rule `projector_view` keeps is kept here first - and so
    the query a whole lesson's projector polls every three seconds builds no
    per-person document only to count it. A row of `_session_checks` is a
    person who checked; `projector_view` turns these numbers into statuses
    the way `_exercise_status` does for one row.
    """
    session = bindparam("session_id", session_id)
    checks = _session_checks(session)
    cohort = _session_cohort(checks, session)
    per_exercise = (
        select(
            checks.c.n,
            func.count().label("n_checked"),
            func.count().filter(checks.c.solved).label("n_solved"),
            func.bool_or(checks.c.checking).label("checking"),
        )
        .group_by(checks.c.n)
        .cte("per_exercise")
    )
    exercises = select(
        func.json_agg(aggregate_order_by(per_exercise.table_valued(), per_exercise.c.n))
    ).scalar_subquery()
    stmt = _session_document(
        session,
        ExerciseSession.c.id,
        ExerciseSession.c.label,
        _session_revealed(session).label("revealed"),
        select(func.count())
        .select_from(cohort)
        .scalar_subquery()
        .label("n_participants"),
        func.coalesce(exercises, _EMPTY_JSON_ARRAY).label("exercises"),
    )
    return (await conn.execute(stmt)).scalar()


async def query_session_cohort_member(conn: AsyncConnection, session_id: int, user_id: int):
    """A person of a session's cohort - `_session_cohort`'s: a
    participant, or anyone with a check in it - with their nickname and
    address, or None. The dashboard's drill-in asks this first, so a user id
    typed into its URL names one of the session's own people or nobody."""
    participated = exists().where(
        ExerciseSessionParticipant.c.session_id == session_id,
        ExerciseSessionParticipant.c.user_id == User.c.id,
    )
    checked = exists().where(
        ExerciseJob.c.session_id == session_id,
        ExerciseJob.c.user_id == User.c.id,
        _is_check(),
    )
    stmt = select(User.c.id, User.c.nickname, User.c.email).where(
        User.c.id == user_id, or_(participated, checked)
    )
    return (await conn.execute(stmt)).first()


async def query_last_session_jobs(
    conn: AsyncConnection, session_id: int, user_id: int, exercise_index: int
) -> dict:
    """This person's latest Run and latest Check of one exercise in a session,
    keyed by kind (`"run"`, `"check"`), either absent if there is none.

    What the student page opens with: each tab's last result, and the newest
    of the two sources as the editor's content when the browser has no draft -
    after a change of computer, say. Nobody else's, by the key.

    Within a kind the latest is the highest id: a queued job is only ever
    replaced while it is the newest of its kind. Across the two, the one
    whose code was sent last is the later `submitted_at` - not the higher id
    nor `enqueued`, which a replacement keeps.
    """
    stmt = (
        select(
            ExerciseJob.c.id,
            ExerciseJob.c.kind,
            ExerciseJob.c.status,
            ExerciseJob.c.version_id,
            ExerciseJob.c.exercise_index,
            ExerciseJob.c.result,
            ExerciseJob.c.source,
            ExerciseJob.c.stdin,
            ExerciseJob.c.submitted_at,
        )
        .distinct(ExerciseJob.c.kind)
        .where(
            ExerciseJob.c.session_id == session_id,
            ExerciseJob.c.user_id == user_id,
            ExerciseJob.c.exercise_index == exercise_index,
            ExerciseJob.c.kind != ExerciseJobKind.build,
        )
        .order_by(ExerciseJob.c.kind, ExerciseJob.c.id.desc())
    )
    return {row.kind.value: row for row in (await conn.execute(stmt)).all()}


async def query_session_job(
    conn: AsyncConnection, session_id: int, job_id: int, user_id: int
):
    """A Run or a Check of a session, for the fragment that polls it - only
    if it is of this session and its asker's own. `query_preview_job`'s rule:
    each is a key, so anybody else's job, or a preview, is simply not found."""
    stmt = select(
        ExerciseJob.c.id,
        ExerciseJob.c.kind,
        ExerciseJob.c.status,
        ExerciseJob.c.version_id,
        ExerciseJob.c.exercise_index,
        ExerciseJob.c.result,
    ).where(
        ExerciseJob.c.id == job_id,
        ExerciseJob.c.user_id == user_id,
        ExerciseJob.c.session_id == session_id,
        ExerciseJob.c.kind != ExerciseJobKind.build,
    )
    return (await conn.execute(stmt)).first()


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
