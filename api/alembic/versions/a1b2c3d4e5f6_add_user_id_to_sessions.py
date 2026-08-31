"""add user_id to sessions

Revision ID: a1b2c3d4e5f6
Revises: 0e0d242438bc
Create Date: 2026-04-28 16:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '0e0d242438bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('user_id', sa.String(length=255), nullable=True))
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_sessions_user_id', table_name='sessions')
    op.drop_column('sessions', 'user_id')
