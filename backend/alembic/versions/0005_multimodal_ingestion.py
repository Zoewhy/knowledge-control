"""multimodal ingestion

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    return column in columns


def upgrade() -> None:
    if not _has_table("document_files"):
        op.create_table(
            "document_files",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "document_id",
                sa.String(length=36),
                sa.ForeignKey("source_documents.id"),
                nullable=False,
            ),
            sa.Column("filename", sa.String(length=300), nullable=False),
            sa.Column("mime", sa.String(length=120), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("storage_path", sa.String(length=1000), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_document_files_document_id", "document_files", ["document_id"])
        op.create_index("ix_document_files_sha256", "document_files", ["sha256"])
    if not _has_column("source_documents", "error_message"):
        op.add_column("source_documents", sa.Column("error_message", sa.Text(), nullable=True))
    if not _has_column("source_documents", "progress"):
        op.add_column(
            "source_documents",
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("source_documents", "file_id"):
        # SQLite 不支持 ALTER 新增外键约束，batch 模式重建表（PG 下等价）
        with op.batch_alter_table("source_documents") as batch:
            batch.add_column(
                sa.Column(
                    "file_id",
                    sa.String(length=36),
                    sa.ForeignKey("document_files.id"),
                    nullable=True,
                )
            )
    if not _has_column("source_segments", "heading_path"):
        op.add_column("source_segments", sa.Column("heading_path", sa.JSON(), nullable=True))
    if not _has_column("source_segments", "segment_type"):
        op.add_column(
            "source_segments",
            sa.Column(
                "segment_type", sa.String(length=20), nullable=False, server_default="text"
            ),
        )


def downgrade() -> None:
    if _has_column("source_segments", "segment_type"):
        op.drop_column("source_segments", "segment_type")
    if _has_column("source_segments", "heading_path"):
        op.drop_column("source_segments", "heading_path")
    document_columns = [
        column
        for column in ("file_id", "progress", "error_message")
        if _has_column("source_documents", column)
    ]
    if document_columns:
        # SQLite 不支持直接 DROP 带外键的列，batch 模式重建表
        with op.batch_alter_table("source_documents") as batch:
            for column in document_columns:
                batch.drop_column(column)
    if _has_table("document_files"):
        op.drop_index("ix_document_files_sha256", table_name="document_files")
        op.drop_index("ix_document_files_document_id", table_name="document_files")
        op.drop_table("document_files")
