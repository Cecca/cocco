from collections.abc import Iterable

from sqlalchemy import bindparam, column, exists, func, insert, or_, select, text, true
from sqlalchemy.dialects.postgresql import (
    JSONB,
    aggregate_order_by,
    insert as pg_insert,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco import schema
from cocco.model import (
    Course,
    Enrollment,
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSession,
    ExerciseSet,
    ExerciseSetVersion,
    ExerciseVersionStatus,
    UserRole,
)
from cocco.repository.judging import notify_queue
from cocco.repository.rules import _EMPTY_JSON_ARRAY


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


def _version_language(version):
    """A version's language; a document that predates the key, or one not
    built yet, is Python's."""
    return func.coalesce(version.c.document["language"].astext, "python")


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
        _version_language(ExerciseSetVersion).label("language"),
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
            ExerciseJob.c.stdin,
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


# How many exercises a version has. Its document is NULL until it is built, and
# a session is only ever on a ready one.
def _n_exercises(version):
    return func.jsonb_array_length(version.c.document["exercises"])
