"""add deep analysis fields

L3b-2（深度分析）：
- `papers.deep_analysis_md`：Markdown 分析全文
- `papers.deep_analyzed_at` / `deep_content_hash` / `deep_status`：
  分析日期、幂等指纹、状态（done/failed）

Revision ID: c5e8f2a7d4b9
Revises: b3d7e9f1c2a4
Create Date: 2026-10-25 00:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e8f2a7d4b9"
down_revision: str | None = "b3d7e9f1c2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("deep_analysis_md", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("deep_analyzed_at", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("deep_content_hash", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("deep_status", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "deep_status")
    op.drop_column("papers", "deep_content_hash")
    op.drop_column("papers", "deep_analyzed_at")
    op.drop_column("papers", "deep_analysis_md")
