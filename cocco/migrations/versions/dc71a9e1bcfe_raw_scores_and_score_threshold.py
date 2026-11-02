"""raw scores and score threshold

Revision ID: dc71a9e1bcfe
Revises: b7d41e2c9a10
Create Date: 2026-11-02 10:51:36.923917

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dc71a9e1bcfe'
down_revision: Union[str, Sequence[str], None] = 'b7d41e2c9a10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('challenge', sa.Column('score_threshold', sa.Float(), nullable=True))

    # Postgres can't drop an enum value in place, so the type is rebuilt without
    # `average_score` (a mean has no meaning once scores are raw sums). The
    # default has to come off first: it is typed as the old enum.
    op.execute("UPDATE challenge SET ranking = 'total_score' WHERE ranking = 'average_score'")
    op.execute("ALTER TABLE challenge ALTER COLUMN ranking DROP DEFAULT")
    op.execute("ALTER TYPE leaderboardranking RENAME TO leaderboardranking_old")
    op.execute("CREATE TYPE leaderboardranking AS ENUM ('running_time', 'total_score')")
    op.execute(
        "ALTER TABLE challenge ALTER COLUMN ranking TYPE leaderboardranking "
        "USING ranking::text::leaderboardranking"
    )
    op.execute("ALTER TABLE challenge ALTER COLUMN ranking SET DEFAULT 'running_time'")
    op.execute("DROP TYPE leaderboardranking_old")


def downgrade() -> None:
    """Downgrade schema."""
    # Challenges that were on `average_score` came back as `total_score` and
    # stay there: the upgrade did not record which ones they were.
    op.execute("ALTER TABLE challenge ALTER COLUMN ranking DROP DEFAULT")
    op.execute("ALTER TYPE leaderboardranking RENAME TO leaderboardranking_new")
    op.execute(
        "CREATE TYPE leaderboardranking AS ENUM "
        "('running_time', 'total_score', 'average_score')"
    )
    op.execute(
        "ALTER TABLE challenge ALTER COLUMN ranking TYPE leaderboardranking "
        "USING ranking::text::leaderboardranking"
    )
    op.execute("ALTER TABLE challenge ALTER COLUMN ranking SET DEFAULT 'running_time'")
    op.execute("DROP TYPE leaderboardranking_new")

    op.drop_column('challenge', 'score_threshold')
