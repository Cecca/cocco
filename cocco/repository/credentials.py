from sqlalchemy import and_, delete, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from cocco.model import ApiToken, User, UserPassword
from cocco.repository.users import _USER_COLUMNS


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
