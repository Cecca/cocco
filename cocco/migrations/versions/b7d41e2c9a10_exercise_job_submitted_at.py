"""exercise job submitted_at, and an index for the student page

One column: when a job's `source` and `stdin` were last written. A queued Run
or Check is rewritten in place when its button is pressed again - same id,
same `enqueued`, deliberately, so it keeps its place in the queue - which
leaves nothing on the row to say which of a student's jobs holds the code they
sent last. The student page needs exactly that, as the editor's content when
the browser has no draft.

The `server_default` is written here by hand, the trap the timestamps
revision describes: `compare_server_default` is off, so autogenerate would add
the column bare and the drift test would not notice. With it, the rows that
exist get `now()` - wrong by however long they have been queued or done, and
harmless, since only a student page's fallback reads it.

And one index, `ix_exercise_job_session_user`: the student page looks up a
person's latest Run and Check of an exercise, and `ix_exercise_job_check`
leaves runs out by design - six months of them is what that lookup would
otherwise scan. No ENUM type, so `downgrade` owes nothing but the drops.

Revision ID: b7d41e2c9a10
Revises: 6e96f99c4b67
Create Date: 2026-10-17 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d41e2c9a10'
down_revision: Union[str, Sequence[str], None] = '6e96f99c4b67'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'exercise_job',
        sa.Column(
            'submitted_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
    )
    op.create_index(
        'ix_exercise_job_session_user',
        'exercise_job',
        ['session_id', 'user_id', 'exercise_index'],
        unique=False,
        postgresql_where=sa.text('session_id IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        'ix_exercise_job_session_user',
        table_name='exercise_job',
        postgresql_where=sa.text('session_id IS NOT NULL'),
    )
    op.drop_column('exercise_job', 'submitted_at')
