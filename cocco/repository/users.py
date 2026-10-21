from collections.abc import Iterable

from sqlalchemy import exists, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco.model import Administrator, User


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
