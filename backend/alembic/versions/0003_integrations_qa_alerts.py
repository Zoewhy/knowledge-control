"""integrations, qa loop, and alert triage

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _create_table(name: str, *columns) -> None:
    if not sa.inspect(op.get_bind()).has_table(name):
        op.create_table(name, *columns)


def _create_index(name: str, table: str, columns: list[str], unique: bool = False) -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in indexes:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    _create_table(
        "integration_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=True),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("agent_task_id", sa.String(length=36), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_task_id"], ["agent_tasks.id"]),
        sa.UniqueConstraint("dedupe_key"),
    )
    _create_index("ix_integration_events_provider", "integration_events", ["provider"])
    _create_index(
        "ix_integration_events_dedupe_key", "integration_events", ["dedupe_key"], unique=True
    )
    _create_index("ix_integration_events_status", "integration_events", ["status"])
    _create_index(
        "ix_integration_events_agent_task_id", "integration_events", ["agent_task_id"]
    )
    _create_table(
        "qa_requirements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_document_id", sa.String(length=36), nullable=True),
        sa.Column("parsed", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_document_id"], ["source_documents.id"]),
    )
    _create_index("ix_qa_requirements_title", "qa_requirements", ["title"])
    _create_table(
        "qa_test_suites",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("requirement_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("testpoints", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["requirement_id"], ["qa_requirements.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"]),
    )
    _create_index("ix_qa_test_suites_requirement_id", "qa_test_suites", ["requirement_id"])
    _create_index("ix_qa_test_suites_task_id", "qa_test_suites", ["task_id"])
    _create_table(
        "qa_test_cases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("suite_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("precondition", sa.Text(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("expected_result", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["suite_id"], ["qa_test_suites.id"]),
    )
    _create_index("ix_qa_test_cases_suite_id", "qa_test_cases", ["suite_id"])
    _create_table(
        "alert_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("fingerprint", sa.String(length=300), nullable=False),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("service", sa.String(length=200), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("triage", sa.JSON(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("agent_task_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_task_id"], ["agent_tasks.id"]),
        sa.UniqueConstraint("fingerprint"),
    )
    _create_index("ix_alert_events_fingerprint", "alert_events", ["fingerprint"], unique=True)
    _create_index("ix_alert_events_title", "alert_events", ["title"])
    _create_index("ix_alert_events_service", "alert_events", ["service"])
    _create_index("ix_alert_events_status", "alert_events", ["status"])
    _create_index("ix_alert_events_agent_task_id", "alert_events", ["agent_task_id"])
    _create_table(
        "incident_records",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("alert_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("root_cause_hypothesis", sa.Text(), nullable=False),
        sa.Column("impact", sa.Text(), nullable=False),
        sa.Column("recommended_actions", sa.JSON(), nullable=False),
        sa.Column("references", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["alert_id"], ["alert_events.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"]),
    )
    _create_index("ix_incident_records_alert_id", "incident_records", ["alert_id"])
    _create_index("ix_incident_records_task_id", "incident_records", ["task_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (
        "incident_records",
        "alert_events",
        "qa_test_cases",
        "qa_test_suites",
        "qa_requirements",
        "integration_events",
    ):
        if inspector.has_table(table):
            op.drop_table(table)
