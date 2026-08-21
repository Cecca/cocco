"""timestamps, and timestamptz everywhere

Fourteen new columns, and the six that already existed reinterpreted.

`created_at` / `updated_at` reach `user`, `course`, `enrollment` and `challenge`;
`created_at` alone reaches `administrator` and `testcase`; `user` also gains
`first_login_at` / `last_login_at`, and `submission` the `started_at` /
`finished_at` that bracket a judge's run. `api_token` already had what it needed
and `testoutcome` wants nothing - `submission.finished_at` is that answer.

Alongside that, every timestamp column in the schema becomes `timestamptz` and
every one of them is written by `now()`. See the comment above `created_at()` in
`model.py` for why: `cocco-web` and `cocco-judge` are two processes with two
clocks, and a naive column only means something once you know which of them
wrote it.

Three hand edits on top of what autogenerate wrote.

1. `ALTER COLUMN ... TYPE timestamptz` from `timestamp` reinterprets the stored
   value in the *session* `TimeZone`, and autogenerate emits no `USING` at all.
   That is not a thing to leave to chance here: the devShell's `pg-start`
   cluster inherits the host's zone while the `postgres:17` container in
   `compose.yaml` runs in UTC, so the same revision would preserve history
   against one and shift it by two hours against the other. `LEGACY_TZ` says it
   out loud, in both directions.

2. Autogenerate could see those six type changes and could *not* see that two of
   them - `api_token.created_at` and `submission.enqueued` - also traded a
   Python-side `default=datetime.now` for a `server_default`, because
   `compare_server_default` is off in `migrate.COMPARE_OPTIONS`. A server default
   is the one drift neither autogenerate nor `test_migrations.py` will ever
   report; every column here that has one is carried by hand, and there are now
   eleven of them rather than three.

3. A NOT NULL `created_at` added to a table with rows in it backfills to the
   moment of the migration, which is a lie about history and the only answer the
   schema has - nothing earlier was ever recorded. Two of them can do better
   from what *is* recorded, and the two `op.execute`s at the end of `upgrade` do.

Revision ID: 0e3c4f7453fe
Revises: 1d25f0f05dcf
Create Date: 2026-08-21 11:38:37.405244

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0e3c4f7453fe'
down_revision: Union[str, Sequence[str], None] = '1d25f0f05dcf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The wall clock the app's `datetime.now()` was reading when the values in the
# six columns below were written.
#
# CHECK THIS BEFORE RUNNING THE MIGRATION against a deployment whose server ran
# in another zone. It is the difference between preserving history and silently
# shifting all of it, and there is no way to detect afterwards which happened.
LEGACY_TZ = "Europe/Rome"

# The columns that predate this revision, with whether they are nullable and
# whether they are also picking up `now()` as a server default (see hand edit 2).
NAIVE_COLUMNS = (
    ("user", "blocked_at", True, False),
    ("api_token", "created_at", False, True),
    ("api_token", "last_used_at", True, False),
    ("course", "archived_at", True, False),
    ("challenge", "archived_at", True, False),
    ("submission", "enqueued", False, True),
)

# What a `created_at` is worth on a row that already existed. Best effort, from
# the only earlier timestamps the schema kept: a person is at least as old as
# their first submission or their first token, and an enrollment at least as old
# as the first thing that person submitted on that course. `LEAST` so a row with
# nothing to go on keeps the migration's own timestamp.
BACKFILL = (
    """
    UPDATE "user" u SET created_at = LEAST(
        u.created_at,
        COALESCE((SELECT min(enqueued)   FROM submission WHERE user_id = u.id), u.created_at),
        COALESCE((SELECT min(created_at) FROM api_token  WHERE user_id = u.id), u.created_at))
    """,
    """
    UPDATE enrollment e SET created_at = LEAST(
        e.created_at,
        COALESCE((SELECT min(s.enqueued)
                    FROM submission s
                    JOIN challenge c ON c.id = s.challenge_id
                   WHERE s.user_id = e.user_id AND c.course_id = e.course_id),
                 e.created_at))
    """,
)


def _created_at() -> sa.Column:
    """`model.created_at()` as this revision spells it. Not imported from the
    model: a revision records the schema as it was at this point in the
    sequence, and would otherwise silently follow `model.py` forward."""
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _updated_at() -> sa.Column:
    """`model.updated_at()`, likewise. `onupdate` is a client-side rule and has
    no DDL, so only the server default is here."""
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("administrator", _created_at())

    op.add_column("course", _created_at())
    op.add_column("course", _updated_at())

    op.add_column("challenge", _created_at())
    op.add_column("challenge", _updated_at())

    op.add_column("enrollment", _created_at())
    op.add_column("enrollment", _updated_at())

    op.add_column("testcase", _created_at())

    op.add_column("user", _created_at())
    op.add_column("user", _updated_at())
    op.add_column(
        "user", sa.Column("first_login_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "user", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column(
        "submission", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "submission", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Hand edit 1 and 2: the `USING`, and the two server defaults autogenerate
    # cannot see.
    for table, column, nullable, gains_default in NAIVE_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=postgresql.TIMESTAMP(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=nullable,
            postgresql_using=f"\"{column}\" AT TIME ZONE '{LEGACY_TZ}'",
            **({"server_default": sa.text("now()")} if gains_default else {}),
        )

    # Hand edit 3. Last, because the first of them reads `submission.enqueued`,
    # which is only comparable with a `timestamptz` once the loop above has run.
    for statement in BACKFILL:
        op.execute(sa.text(statement))


def downgrade() -> None:
    """Downgrade schema."""
    for table, column, nullable, gains_default in reversed(NAIVE_COLUMNS):
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            type_=postgresql.TIMESTAMP(),
            existing_nullable=nullable,
            postgresql_using=f"\"{column}\" AT TIME ZONE '{LEGACY_TZ}'",
            **({"server_default": None} if gains_default else {}),
        )

    op.drop_column("submission", "finished_at")
    op.drop_column("submission", "started_at")

    op.drop_column("user", "last_login_at")
    op.drop_column("user", "first_login_at")
    op.drop_column("user", "updated_at")
    op.drop_column("user", "created_at")

    op.drop_column("testcase", "created_at")

    op.drop_column("enrollment", "updated_at")
    op.drop_column("enrollment", "created_at")

    op.drop_column("challenge", "updated_at")
    op.drop_column("challenge", "created_at")

    op.drop_column("course", "updated_at")
    op.drop_column("course", "created_at")

    op.drop_column("administrator", "created_at")
