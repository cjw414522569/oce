"""add blobs.completed_at for queue throughput stats

Revision ID: k6e7f8a9b0c1
Revises: j5d6e7f8a9b0
Create Date: 2026-09-16 06:20:00.000000

存量 ready blob 无真实完成时间，回填 created_at 作近似值（一次性打点，
新数据起为 worker 真实完成时间）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'k6e7f8a9b0c1'
down_revision: Union[str, None] = 'j5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('blobs', sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_blobs_completed_at', 'blobs', ['completed_at'])
    op.execute(
        "UPDATE blobs SET completed_at = created_at WHERE status = 'ready' AND completed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index('ix_blobs_completed_at', table_name='blobs')
    op.drop_column('blobs', 'completed_at')
