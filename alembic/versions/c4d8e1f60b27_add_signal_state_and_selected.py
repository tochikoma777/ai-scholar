"""add signal state machine + selected

L1-B：
- `papers.signal_state / signal_next_at / signal_last_at / signal_revived /
  prescreen_reason`：采样状态机（★ 持久状态，复用 papers 表）
- `papers.selected`：选集标记（export 默认只导出 selected=1）

★ `signal_state` 故意**不给** server_default：迁移前的历史行留 NULL，
  下一次 `signals` 先补跑零成本预筛，避免把淘汰论文直接当 pending。

Revision ID: c4d8e1f60b27
Revises: b7c1a9d24f31
Create Date: 2026-10-24 00:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d8e1f60b27"
down_revision: str | None = "b7c1a9d24f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("signal_state", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("signal_next_at", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("signal_last_at", sa.String(), nullable=True))
    op.add_column(
        "papers",
        sa.Column("signal_revived", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column("papers", sa.Column("prescreen_reason", sa.String(), nullable=True))
    op.add_column(
        "papers",
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("papers", "selected")
    op.drop_column("papers", "prescreen_reason")
    op.drop_column("papers", "signal_revived")
    op.drop_column("papers", "signal_last_at")
    op.drop_column("papers", "signal_next_at")
    op.drop_column("papers", "signal_state")
