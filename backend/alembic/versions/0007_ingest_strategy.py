"""ingest strategy platform

Revision ID: 0007
Revises: 0006
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

DEFAULT_POLICY = {
    "name": "default",
    "doc_pattern": "*",
    "parser_strategy": "auto",
    "chunk_strategy": "heading",
    "chunk_params": {"max_tokens": 512, "overlap_tokens": 50},
    "table_mode": "chunk",
    "min_text_chars": 100,
    "auto_compile": True,
}


def _has_table(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    return column in columns


def _seed_default_policy() -> None:
    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT COUNT(*) FROM ingest_policies WHERE name = 'default'")
    ).scalar()
    if existing:
        return
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    bind.execute(
        sa.text(
            "INSERT INTO ingest_policies (id, name, doc_pattern, parser_strategy,"
            " chunk_strategy, chunk_params, table_mode, min_text_chars, auto_compile,"
            " version, created_at, updated_at) VALUES (:id, :name, :doc_pattern,"
            " :parser_strategy, :chunk_strategy, :chunk_params, :table_mode,"
            " :min_text_chars, :auto_compile, :version, :created_at, :updated_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            **DEFAULT_POLICY,
            "chunk_params": json.dumps(DEFAULT_POLICY["chunk_params"]),
            "version": 1,
            "created_at": now,
            "updated_at": now,
        },
    )


def upgrade() -> None:
    if not _has_table("ingest_policies"):
        op.create_table(
            "ingest_policies",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("name", sa.String(length=200), nullable=False, unique=True),
            sa.Column("doc_pattern", sa.String(length=300), nullable=False, server_default="*"),
            sa.Column(
                "parser_strategy", sa.String(length=40), nullable=False, server_default="auto"
            ),
            sa.Column(
                "chunk_strategy", sa.String(length=40), nullable=False, server_default="heading"
            ),
            sa.Column("chunk_params", sa.JSON(), nullable=True),
            sa.Column("table_mode", sa.String(length=40), nullable=False, server_default="chunk"),
            sa.Column("min_text_chars", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("auto_compile", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ingest_policies_name", "ingest_policies", ["name"], unique=True)
    if not _has_table("strategy_experiments"):
        op.create_table(
            "strategy_experiments",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("name", sa.String(length=300), nullable=False),
            sa.Column("document_ids", sa.JSON(), nullable=True),
            sa.Column("eval_group", sa.String(length=60), nullable=False, server_default="default"),
            sa.Column("target_metrics", sa.JSON(), nullable=True),
            sa.Column("applied_run_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if not _has_table("strategy_experiment_runs"):
        op.create_table(
            "strategy_experiment_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "experiment_id",
                sa.String(length=36),
                sa.ForeignKey("strategy_experiments.id"),
                nullable=False,
            ),
            sa.Column("policy_snapshot", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="pending"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("metrics", sa.JSON(), nullable=True),
            sa.Column("eval_run_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_strategy_experiment_runs_experiment_id",
            "strategy_experiment_runs",
            ["experiment_id"],
        )
    if not _has_column("evaluation_cases", "eval_group"):
        op.add_column(
            "evaluation_cases",
            sa.Column(
                "eval_group",
                sa.String(length=60),
                nullable=False,
                server_default="default",
            ),
        )
    _seed_default_policy()


def downgrade() -> None:
    if _has_column("evaluation_cases", "eval_group"):
        op.drop_column("evaluation_cases", "eval_group")
    if _has_table("strategy_experiment_runs"):
        op.drop_index(
            "ix_strategy_experiment_runs_experiment_id", table_name="strategy_experiment_runs"
        )
        op.drop_table("strategy_experiment_runs")
    if _has_table("strategy_experiments"):
        op.drop_table("strategy_experiments")
    if _has_table("ingest_policies"):
        op.drop_index("ix_ingest_policies_name", table_name="ingest_policies")
        op.drop_table("ingest_policies")
