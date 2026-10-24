"""add grading fields

L3b-1（文献定级）：
- `papers.tier`：1/2/3，NULL = 未定级
- `papers.grade_text` / `grade_confidence` / `graded_at` / `grade_content_hash`：
  grading.j2 原始文本、置信度、定级日期、幂等指纹

Revision ID: b3d7e9f1c2a4
Revises: a9e2c5b7d3f1
Create Date: 2026-10-24 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7e9f1c2a4"
down_revision: str | None = "a9e2c5b7d3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("tier", sa.Integer(), nullable=True))
    op.add_column("papers", sa.Column("grade_text", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("grade_confidence", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("graded_at", sa.String(), nullable=True))
    op.add_column("papers", sa.Column("grade_content_hash", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "grade_content_hash")
    op.drop_column("papers", "graded_at")
    op.drop_column("papers", "grade_confidence")
    op.drop_column("papers", "grade_text")
    op.drop_column("papers", "tier")
