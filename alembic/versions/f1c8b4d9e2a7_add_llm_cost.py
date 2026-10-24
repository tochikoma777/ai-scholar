"""add llm_usage cost fields

L3a-1（LLM 记账闭环）：
- `llm_usage.cost_cny`：本次调用的估算成本（元）
- `llm_usage.cache_hit`：是否命中 prompt 缓存

Revision ID: f1c8b4d9e2a7
Revises: e5a7c9d02b14
Create Date: 2026-10-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1c8b4d9e2a7"
down_revision: str | None = "e5a7c9d02b14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("cost_cny", sa.Float(), nullable=True))
    op.add_column(
        "llm_usage",
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "cache_hit")
    op.drop_column("llm_usage", "cost_cny")
