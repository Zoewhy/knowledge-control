"""evaluation case question type

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    return column in columns


def upgrade() -> None:
    if not _has_column("evaluation_cases", "question_type"):
        op.add_column(
            "evaluation_cases",
            sa.Column(
                "question_type",
                sa.String(length=40),
                nullable=False,
                server_default="general",
            ),
        )


def downgrade() -> None:
    if _has_column("evaluation_cases", "question_type"):
        op.drop_column("evaluation_cases", "question_type")
