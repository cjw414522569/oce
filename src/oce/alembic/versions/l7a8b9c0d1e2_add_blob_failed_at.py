"""add blobs.failed_at for failure-window stats

Revision ID: l7a8b9c0d1e2
Revises: k6e7f8a9b0c1
Create Date: 2026-09-16 07:10:00.000000

存量 error blob 无真实失败时间，回填 last_seen 作近似值（其 staging 已被
清理，last_seen 是最近一次活动痕迹）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'l7a8b9c0d1e2'
down_revision: Union[str, None] = 'k6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('blobs', sa.Column('failed_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_blobs_failed_at', 'blobs', ['failed_at'])
    op.execute(
        "UPDATE blobs SET failed_at = last_seen WHERE status = 'error' AND failed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index('ix_blobs_failed_at', table_name='blobs')
    op.drop_column('blobs', 'failed_at')
