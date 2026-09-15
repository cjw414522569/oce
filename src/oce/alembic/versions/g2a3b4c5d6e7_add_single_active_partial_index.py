"""add single-active partial unique index on user_api_keys

Revision ID: g2a3b4c5d6e7
Revises: f1b2c3d4e5f6
Create Date: 2026-09-15 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g2a3b4c5d6e7'
down_revision: Union[str, None] = 'f1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 部分唯一索引：同 user 至多一行 status='active'，并发轮换在 DB 层暴露冲突
    op.create_index(
        'uq_user_api_keys_single_active',
        'user_api_keys',
        ['user_id'],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index('uq_user_api_keys_single_active', table_name='user_api_keys')
