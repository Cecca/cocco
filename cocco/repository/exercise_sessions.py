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
    update,
)
from sqlalchemy.dialects.postgresql import (
    JSONB,
    aggregate_order_by,
    insert as pg_insert,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco.model import (
    ExerciseJob,
    ExerciseJobKind,
    ExerciseJobStatus,
    ExerciseSession,
    ExerciseSessionMode,
    ExerciseSessionParticipant,
    ExerciseSessionReveal,
    ExerciseSet,
    ExerciseSetVersion,
    User,
)
from cocco.repository.exercise_sets import _n_exercises, _version_language, _version_number
from cocco.repository.rules import _EMPTY_JSON_ARRAY, _json_object


# --- Exercise sessions ---------------------------------------------------------
#
# A session pins a version of a set and carries the pacing; see the plan's
# "Sessions carry pacing and progress". Every write below is a single
# conditional statement, the way `block_user` refuses an administrator: the
# rule ("only while open", "only from where the page said it was") is in the
# `WHERE` clause, so two teachers pressing the same button at once cannot both
# act on what neither of them saw.


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


async def query_version_language(conn: AsyncConnection, version_id: int) -> str | None:
    """The language a version's exercises are written in; None while it has
    no document. A session's language is fixed, so the version switch compares
    these; read it under the session's row lock with the rest of its refusals."""
    stmt = select(_version_language(ExerciseSetVersion)).where(
        ExerciseSetVersion.c.id == version_id,
        ExerciseSetVersion.c.document.is_not(None),
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
            _version_language(ExerciseSetVersion).label("language"),
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
               coalesce(v.document ->> 'language', 'python') AS language,
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
    The session's label and pacing, the set and version it is on (for the
    link to the staff preview), the titles, the reveals, the size of the
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
        ExerciseSession.c.set_id,
        ExerciseSession.c.version_id,
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
        ExerciseJob.c.stdin,
    ).where(
        ExerciseJob.c.id == job_id,
        ExerciseJob.c.user_id == user_id,
        ExerciseJob.c.session_id == session_id,
        ExerciseJob.c.kind != ExerciseJobKind.build,
    )
    return (await conn.execute(stmt)).first()
