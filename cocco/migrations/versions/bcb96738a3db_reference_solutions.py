"""reference solutions

Revision ID: bcb96738a3db
Revises: dc71a9e1bcfe
Create Date: 2026-11-12 19:25:37.574421

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'bcb96738a3db'
down_revision: Union[str, Sequence[str], None] = 'dc71a9e1bcfe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # `add_column` does not create the enum type, so it is created by hand.
    visibility = postgresql.ENUM('hidden', 'results', 'source', name='referencevisibility')
    visibility.create(op.get_bind(), checkfirst=False)
    op.add_column('submission', sa.Column('reference_label', sa.String(), nullable=True))
    op.add_column('submission', sa.Column('reference_visibility', postgresql.ENUM('hidden', 'results', 'source', name='referencevisibility', create_type=False), nullable=True))
    op.create_check_constraint('submission_reference_columns_together', 'submission', '(reference_label IS NULL) = (reference_visibility IS NULL)')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('submission_reference_columns_together', 'submission', type_='check')
    op.drop_column('submission', 'reference_visibility')
    op.drop_column('submission', 'reference_label')
    postgresql.ENUM(name='referencevisibility').drop(op.get_bind(), checkfirst=False)
