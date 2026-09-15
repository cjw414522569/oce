"""add key_plaintext column to user_api_keys

Revision ID: h3b4c5d6e7f8
Revises: g2a3b4c5d6e7
Create Date: 2026-09-15 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'h3b4c5d6e7f8'
down_revision: Union[str, None] = 'g2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 运维选择：用户 key 在门户常显 → 需要回放明文。可空列兼容存量行
    # （存量 key 只有哈希，轮换一次后即有明文）。
    op.add_column('user_api_keys', sa.Column('key_plaintext', sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column('user_api_keys', 'key_plaintext')
