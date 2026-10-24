"""add paper_references + papers.similar_at

L1-C：
- `paper_references(paper_id, ref_external_id)`：引用列表，**只落 db、只存外部 ID**
- `idx_refs_external`：文献耦合的倒排索引（按被引 id 分桶，避免全库两两比较）
- `papers.similar_at`：TF-IDF 增量计算标记（NULL = 尚未参与比对）

Revision ID: e5a7c9d02b14
Revises: c4d8e1f60b27
Create Date: 2026-10-24 01:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a7c9d02b14"
down_revision: str | None = "c4d8e1f60b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_references",
        sa.Column("paper_id", sa.String(), nullable=False),
        sa.Column("ref_external_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"]),
        sa.PrimaryKeyConstraint("paper_id", "ref_external_id"),
    )
    op.create_index("idx_refs_external", "paper_references", ["ref_external_id"])
    op.add_column("papers", sa.Column("similar_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "similar_at")
    op.drop_index("idx_refs_external", table_name="paper_references")
    op.drop_table("paper_references")
