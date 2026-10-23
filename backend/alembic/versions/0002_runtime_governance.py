"""runtime governance, knowledge graph, and agent artifacts

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _create_table(name: str, *columns) -> None:
    if not sa.inspect(op.get_bind()).has_table(name):
        op.create_table(name, *columns)


def _create_index(name: str, table: str, columns: list[str], unique: bool = False) -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    if "error_detail" not in _columns("agent_tasks"):
        op.add_column(
            "agent_tasks",
            sa.Column("error_detail", sa.JSON(), nullable=False, server_default="{}"),
        )
    _create_table(
        "knowledge_entities",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("normalized_name"),
    )
    _create_index(
        "ix_knowledge_entities_normalized_name",
        "knowledge_entities",
        ["normalized_name"],
        unique=True,
    )
    _create_table(
        "wiki_section_entities",
        sa.Column("section_id", sa.String(length=36), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["section_id"], ["wiki_sections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["entity_id"], ["knowledge_entities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("section_id", "entity_id"),
    )
    _create_table(
        "knowledge_relations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("subject_entity_id", sa.String(length=36), nullable=False),
        sa.Column("object_entity_id", sa.String(length=36), nullable=False),
        sa.Column("predicate", sa.String(length=80), nullable=False),
        sa.Column("claim_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subject_entity_id"], ["knowledge_entities.id"]),
        sa.ForeignKeyConstraint(["object_entity_id"], ["knowledge_entities.id"]),
        sa.ForeignKeyConstraint(["claim_id"], ["source_claims.id"]),
        sa.UniqueConstraint(
            "subject_entity_id",
            "object_entity_id",
            "predicate",
            "claim_id",
            name="uq_knowledge_relation_evidence",
        ),
    )
    _create_index(
        "ix_knowledge_relations_subject_entity_id",
        "knowledge_relations",
        ["subject_entity_id"],
    )
    _create_index(
        "ix_knowledge_relations_object_entity_id", "knowledge_relations", ["object_entity_id"]
    )
    _create_index("ix_knowledge_relations_claim_id", "knowledge_relations", ["claim_id"])
    _create_table(
        "agent_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"]),
    )
    _create_index("ix_agent_events_task_id", "agent_events", ["task_id"])
    _create_index("ix_agent_events_event_type", "agent_events", ["event_type"])
    _create_table(
        "agent_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("artifact_type", sa.String(length=60), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"]),
        sa.ForeignKeyConstraint(["tool_call_id"], ["tool_call_records.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["source_documents.id"]),
    )
    _create_index("ix_agent_artifacts_task_id", "agent_artifacts", ["task_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (
        "agent_artifacts",
        "agent_events",
        "knowledge_relations",
        "wiki_section_entities",
        "knowledge_entities",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
    if "error_detail" in _columns("agent_tasks"):
        op.drop_column("agent_tasks", "error_detail")
