"""add enrich queue + note_path fields

L1-A：
- `papers.note_path`：vault 笔记相对路径（NULL = 无笔记），双轨渲染据此判断
- `papers.enrich_state / enrich_attempts / enrich_last_at / enrich_error`：
  ★ 补齐队列**复用 papers 表**，不新建队列表

Revision ID: b7c1a9d24f31
Revises: e3f28f5e8a34
Create Date: 2026-10-23 23:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c1a9d24f31"
down_revision: str | None = "e3f28f5e8a34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("note_path", sa.String(), nullable=True))
    op.add_column(
        "papers",
        sa.Column("enrich_state", sa.String(), nullable=False, server_default="pending"),
    )
    op.add_column(
        "papers",
        sa.Column("enrich_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("papers", sa.Column("enrich_last_at", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("enrich_error", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "enrich_error")
    op.drop_column("papers", "enrich_last_at")
    op.drop_column("papers", "enrich_attempts")
    op.drop_column("papers", "enrich_state")
    op.drop_column("papers", "note_path")
