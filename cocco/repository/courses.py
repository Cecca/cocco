from sqlalchemy import and_, bindparam, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import aggregate_order_by, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco.model import Challenge, Course, Enrollment, Submission, UserRole
from cocco.repository.rules import (
    _EMPTY_JSON_ARRAY,
    _challenge_number,
    _challenge_visible,
    _effective_archived_at,
)


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


def _course_challenges(course_id, user_id):
    """The challenges of one course, as a CTE in which each carries what its
    card on the course page decides its badges with:

    - `effective_archived_at`, so a card under an archived course greys itself
      without the template having to know that a course can archive a
      challenge. `challenge.archived_at` rides along as the challenge's own
      state, which only the challenge editor's danger zone reads;
    - `is_visible`, whether the class may see it at all, as against whether
      they may still submit to it. `visible_from` rides along too: a scheduled
      challenge's card has to print *when*;
    - `n_mine`, how many times the viewer submitted, which is what a student's
      "Submitted" badge means;
    - `n_submitters`, how many distinct people did, which is the same badge as
      course staff see it - and what tells them which challenge the class has
      actually started.

    It carries `number` (`_challenge_number`) rather than leaving the template
    to count with `loop.index`, and that is load-bearing now that the list can
    be filtered: a student is shown neither the hidden challenges nor - in the
    editor's copy of this list - anything staff have not published, so counting
    rows on the page would print "Challenge 03" to a student in front of what
    staff call 04. The window runs over every challenge of the course, inside
    the CTE, *before* `_listed_challenges` filters it. It is the same rank
    `query_challenge` computes.
    """
    submitted = (
        select(
            Submission.c.challenge_id,
            func.count().filter(Submission.c.user_id == user_id).label("n_mine"),
            func.count(Submission.c.user_id.distinct()).label("n_submitters"),
        )
        .join(Challenge, Challenge.c.id == Submission.c.challenge_id)
        .where(Challenge.c.course_id == course_id)
        .group_by(Submission.c.challenge_id)
        .cte("submitted")
    )
    # LEFT JOIN and coalesce: a challenge nobody has submitted to has no row
    # in `submitted` at all, and it is still a challenge the page must list.
    return (
        select(
            Challenge,
            _effective_archived_at().label("effective_archived_at"),
            _challenge_visible().label("is_visible"),
            _challenge_number().label("number"),
            func.coalesce(submitted.c.n_mine, 0).label("n_mine"),
            func.coalesce(submitted.c.n_submitters, 0).label("n_submitters"),
        )
        .select_from(
            Challenge.outerjoin(Course, Course.c.id == Challenge.c.course_id).outerjoin(
                submitted, submitted.c.challenge_id == Challenge.c.id
            )
        )
        .where(Challenge.c.course_id == course_id)
        .cte("challenges")
    )


def _listed_challenges(challenges, include_hidden: bool):
    """`_course_challenges` as the JSON array a page lists, in course order,
    with only the rows this caller may be shown. Staff pass `include_hidden`
    and get the lot, because that is the list they publish from; everybody
    else gets the visible challenges *and* the scheduled ones, whose card is a
    teaser with no link - so the class can see what is coming - and never a
    hidden one, which they are not told exists.

    Filtered here, outside the CTE rather than in its WHERE, so that `number`
    is already ranked over the whole course by the time anything is dropped.
    """
    stmt = select(
        func.coalesce(
            func.json_agg(
                aggregate_order_by(
                    challenges.table_valued(),
                    challenges.c.position,
                    challenges.c.id,
                )
            ),
            _EMPTY_JSON_ARRAY,
        )
    )
    if not include_hidden:
        stmt = stmt.where(challenges.c.visible_from.is_not(None))
    return stmt


async def query_course_info(
    conn: AsyncConnection, course_id: int, user_id: int, include_hidden: bool = False
):
    """The course, its description and its challenges as the page lists them.

    One document rather than a query per card: see `_course_challenges` for
    what each challenge carries. The course editor reads the same thing - it
    shows the same list, plus the arrows - so every reorder re-renders from
    here and cannot drift from what is stored.

    `include_hidden` is what makes the two lists differ, and it defaults to the
    safe answer: a caller who forgets it shows a student's list, never a
    teacher's. See `_listed_challenges`.
    """
    course = bindparam("course_id", course_id)
    challenges = _course_challenges(course, bindparam("user_id", user_id))
    # A subquery, rather than a join and a GROUP BY: a course with no
    # challenges yet is a real course - it is the one whose page course staff
    # open to create the first challenge - and it must come back with an empty
    # list, not as no row at all, which the route would report as a 404.
    c = (
        select(
            Course,
            _listed_challenges(challenges, include_hidden)
            .scalar_subquery()
            .label("challenges"),
        )
        .where(Course.c.id == course)
        .cte("c")
    )
    stmt = select(func.row_to_json(c.table_valued()))
    return (await conn.execute(stmt)).scalar()


async def query_course_challenges(
    conn: AsyncConnection, course_id: int, user_id: int, include_hidden: bool = False
):
    """Just the list `query_course_info` nests, for the fragment the reorder
    arrows swap: a move changes the order of the cards and nothing about the
    course itself."""
    challenges = _course_challenges(
        bindparam("course_id", course_id), bindparam("user_id", user_id)
    )
    stmt = _listed_challenges(challenges, include_hidden)
    return (await conn.execute(stmt)).scalar()
