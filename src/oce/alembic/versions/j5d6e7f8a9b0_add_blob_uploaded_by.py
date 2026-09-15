"""add blobs.uploaded_by for per-user token attribution

Revision ID: j5d6e7f8a9b0
Revises: i4c5d6e7f8a9
Create Date: 2026-09-15 20:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'j5d6e7f8a9b0'
down_revision: Union[str, None] = 'i4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'blobs',
        sa.Column('uploaded_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
    )
    op.create_index('ix_blobs_uploaded_by', 'blobs', ['uploaded_by'])


def downgrade() -> None:
    op.drop_index('ix_blobs_uploaded_by', table_name='blobs')
    op.drop_column('blobs', 'uploaded_by')
