"""add users and user api keys for multi-tenant access

Revision ID: f1b2c3d4e5f6
Revises: e2f4a6c8d0b1
Create Date: 2026-09-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1b2c3d4e5f6'
down_revision: Union[str, None] = 'e2f4a6c8d0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('linuxdo_id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(length=128), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=True),
        sa.Column('avatar_template', sa.String(length=512), nullable=True),
        sa.Column('trust_level', sa.Integer(), server_default='0', nullable=False),
        sa.Column('active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('silenced', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('linuxdo_id', name='uq_users_linuxdo_id'),
    )

    op.create_table(
        'user_api_keys',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('key_last4', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key_hash', name='uq_user_api_keys_key_hash'),
    )
    op.create_index('ix_user_api_keys_user_id', 'user_api_keys', ['user_id'])
    op.create_index('ix_user_api_keys_status', 'user_api_keys', ['status'])

    # 可空列 + 索引：SQLite 直接 add_column 即可，batch_alter 仅约束/NOT NULL 变更才需要
    op.add_column('api_call_metrics', sa.Column('user_id', sa.Integer(), nullable=True))
    op.create_index('ix_api_call_metrics_user_id', 'api_call_metrics', ['user_id'])
    op.add_column('token_usage_metrics', sa.Column('user_id', sa.Integer(), nullable=True))
    op.create_index('ix_token_usage_metrics_user_id', 'token_usage_metrics', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_token_usage_metrics_user_id', table_name='token_usage_metrics')
    op.drop_column('token_usage_metrics', 'user_id')
    op.drop_index('ix_api_call_metrics_user_id', table_name='api_call_metrics')
    op.drop_column('api_call_metrics', 'user_id')
    op.drop_index('ix_user_api_keys_status', table_name='user_api_keys')
    op.drop_index('ix_user_api_keys_user_id', table_name='user_api_keys')
    op.drop_table('user_api_keys')
    op.drop_table('users')
