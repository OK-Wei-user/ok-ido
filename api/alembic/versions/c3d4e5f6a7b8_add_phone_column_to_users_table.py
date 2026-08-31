"""add phone column to users table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-29 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('users')]

    if 'phone' not in columns:
        op.add_column('users', sa.Column(
            'phone', sa.String(length=20),
            nullable=False, server_default=sa.text("''"),
        ))
        op.create_index('ix_users_phone', 'users', ['phone'])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('users')]

    if 'phone' in columns:
        op.drop_index('ix_users_phone', table_name='users')
        op.drop_column('users', 'phone')
