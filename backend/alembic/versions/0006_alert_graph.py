"""alert graph relation confidence

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    return column in columns


def upgrade() -> None:
    if not _has_column("knowledge_relations", "confidence"):
        op.add_column(
            "knowledge_relations",
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        )


def downgrade() -> None:
    if _has_column("knowledge_relations", "confidence"):
        op.drop_column("knowledge_relations", "confidence")
