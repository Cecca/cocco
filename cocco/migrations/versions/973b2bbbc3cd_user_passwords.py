"""user passwords

`[auth] password_login` gives a deployment a second way in - an address and a
password - for the case Google OIDC cannot serve: no Workspace, or no wish to
register an OAuth client. This is the whole of its schema.

A table rather than a nullable column on `user`, and the reason is
`_USER_COLUMNS` in `repository.py`: that list is read on every single request,
and a credential has no business travelling with the identity every route
already holds. Keyed on `user.id` for `api_token`'s reason - a password belongs
to an account that exists, unlike `administrator`, which is keyed on an address
so it can name somebody who has never signed in.

No row means no password, which is what a Google-only account looks like and
what an account looks like before anybody has claimed it. So there is nothing
to backfill: every account this revision meets keeps signing in exactly as it
did, and `password_login` defaults to false in any case.

Both timestamps carry `server_default=now()` here, as every timestamp in this
schema does. They came out of autogenerate that way because this is a *new*
table and `create_table` renders a column's own default - the hand edit
`env.py` warns about is for a default that *changed* on a column that already
existed, which `compare_server_default = False` makes invisible.

Revision ID: 973b2bbbc3cd
Revises: 12c83cecdeb2
Create Date: 2026-09-04 14:34:44.139532

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '973b2bbbc3cd'
down_revision: Union[str, Sequence[str], None] = '12c83cecdeb2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('user_password',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('password_hash', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('user_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    # No enum type to drop by hand: this revision creates none. See the
    # baseline's `ENUM_TYPES` loop for the case that does.
    op.drop_table('user_password')
