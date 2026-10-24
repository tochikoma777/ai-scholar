"""add enhance fields

L3a-2（LLM 增强）：
- `papers.one_line / why / method_tags_json / enhanced_at / enhance_content_hash`：
  一句话 / 为什么重要 / 方法标签 + 增强时间 + 增强时的内容身份
- `paper_links.interpretation`：强相似边的 LLM 关联解读

Revision ID: a9e2c5b7d3f1
Revises: f1c8b4d9e2a7
Create Date: 2026-10-24 13:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9e2c5b7d3f1"
down_revision: str | None = "f1c8b4d9e2a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("one_line", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("why", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("method_tags_json", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("enhanced_at", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("enhance_content_hash", sa.String(), nullable=True))
    op.add_column("paper_links", sa.Column("interpretation", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_links", "interpretation")
    op.drop_column("papers", "enhance_content_hash")
    op.drop_column("papers", "enhanced_at")
    op.drop_column("papers", "method_tags_json")
    op.drop_column("papers", "why")
    op.drop_column("papers", "one_line")
