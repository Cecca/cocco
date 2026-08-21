"""challenge visibility

One nullable column, and one hand-added line that matters more than it does.

`challenge.visible_from` is the challenge's own end of the life cycle
`archived_at` closes: NULL is hidden, a past instant is visible, a future one
is a challenge that opens by itself at that moment. NULL is therefore the state
a *new* challenge is created in - see `insert_challenge` - and that is exactly
why the backfill below is not optional. Every challenge that exists when this
revision runs has been visible since the day it was created, and adding the
column without writing to it would empty every course page in the deployment
the moment the app came back up.

`created_at` rather than `now()` for the backfill: the sentence a badge prints
is "visible since", and for a challenge that has been live for a term, the day
of this migration is not the truth.

The column carries no `server_default`, which is what keeps it out of the trap
the previous revision's docstring describes - `compare_server_default` is off
in `migrate.COMPARE_OPTIONS`, so a default would be invisible to autogenerate
and to the drift test and would have to be carried by hand forever after. It
adds no ENUM type either, so `downgrade` owes nothing but the drop.

Revision ID: 12c83cecdeb2
Revises: 0e3c4f7453fe
Create Date: 2026-08-21 14:55:53.792673

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '12c83cecdeb2'
down_revision: Union[str, Sequence[str], None] = '0e3c4f7453fe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "challenge", sa.Column("visible_from", sa.DateTime(timezone=True), nullable=True)
    )
    # Hand-added, and the reason this revision exists rather than being a
    # one-line autogenerate: NULL is the new spelling of "hidden", so leaving
    # these rows NULL would hide every challenge in the deployment from every
    # student. They have all been visible since they were created.
    op.execute("UPDATE challenge SET visible_from = created_at")


def downgrade() -> None:
    """Downgrade schema."""
    # Nothing to undo about the backfill - the column it wrote is going.
    op.drop_column("challenge", "visible_from")
