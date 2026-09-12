"""Persist repository setup admission and task execution holds.

Revision ID: c7d9e1a234b5
Revises: ba862235dfe7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d9e1a234b5"
down_revision: str | None = "ba862235dfe7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repo", sa.Column("launch_paused", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "task", sa.Column("launch_paused", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("task", sa.Column("launch_pause_reason", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task") as batch:
        batch.drop_column("launch_pause_reason")
        batch.drop_column("launch_paused")
    with op.batch_alter_table("repo") as batch:
        batch.drop_column("launch_paused")
