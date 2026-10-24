from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import settings
from .database import Base

EmbeddingType = Vector(settings.bailian_embedding_dimensions).with_variant(JSON, "sqlite")


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReviewStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class PatchAction(str, enum.Enum):
    create_page = "create_page"
    append_section = "append_section"
    update_section = "update_section"
    source_only = "source_only"
    duplicate = "duplicate"


class TaskStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    waiting_approval = "waiting_approval"
    succeeded = "succeeded"
    failed = "failed"


class SourceDocument(Base):
    __tablename__ = "source_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), index=True)
    source_type: Mapped[str] = mapped_column(String(40), default="manual")
    source_uri: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    owner: Mapped[str] = mapped_column(String(120), default="unknown")
    checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default="processed")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    segments: Mapped[list["SourceSegment"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class SourceSegment(Base):
    __tablename__ = "source_segments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("source_documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    heading: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[Optional[list]] = mapped_column(EmbeddingType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    document: Mapped[SourceDocument] = relationship(back_populates="segments")
    claims: Mapped[list["SourceClaim"]] = relationship(
        back_populates="segment", cascade="all, delete-orphan"
    )


class SourceClaim(Base):
    __tablename__ = "source_claims"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(ForeignKey("source_segments.id"), index=True)
    statement: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(200), default="general")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(40), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    segment: Mapped[SourceSegment] = relationship(back_populates="claims")
    citations: Mapped[list["WikiCitation"]] = relationship(back_populates="claim")


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    owner: Mapped[str] = mapped_column(String(120), default="unknown")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    sections: Mapped[list["WikiSection"]] = relationship(
        back_populates="page", cascade="all, delete-orphan", order_by="WikiSection.ordinal"
    )


class WikiSection(Base):
    __tablename__ = "wiki_sections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("wiki_pages.id"), index=True)
    heading: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[Optional[list]] = mapped_column(EmbeddingType, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    page: Mapped[WikiPage] = relationship(back_populates="sections")
    citations: Mapped[list["WikiCitation"]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )


class WikiCitation(Base):
    __tablename__ = "wiki_citations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    section_id: Mapped[str] = mapped_column(ForeignKey("wiki_sections.id"), index=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("source_claims.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    section: Mapped[WikiSection] = relationship(back_populates="citations")
    claim: Mapped[SourceClaim] = relationship(back_populates="citations")


class KnowledgeEntity(Base):
    __tablename__ = "knowledge_entities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(40), default="concept")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WikiSectionEntity(Base):
    __tablename__ = "wiki_section_entities"
    section_id: Mapped[str] = mapped_column(
        ForeignKey("wiki_sections.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_entities.id", ondelete="CASCADE"), primary_key=True
    )


class KnowledgeRelation(Base):
    __tablename__ = "knowledge_relations"
    __table_args__ = (
        UniqueConstraint(
            "subject_entity_id",
            "object_entity_id",
            "predicate",
            "claim_id",
            name="uq_knowledge_relation_evidence",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subject_entity_id: Mapped[str] = mapped_column(ForeignKey("knowledge_entities.id"), index=True)
    object_entity_id: Mapped[str] = mapped_column(ForeignKey("knowledge_entities.id"), index=True)
    predicate: Mapped[str] = mapped_column(String(80), default="related_to")
    claim_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("source_claims.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewTask(Base):
    __tablename__ = "review_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    claim_id: Mapped[str] = mapped_column(ForeignKey("source_claims.id"), index=True)
    target_page_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("wiki_pages.id"), nullable=True
    )
    target_section_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("wiki_sections.id"), nullable=True
    )
    action: Mapped[PatchAction] = mapped_column(Enum(PatchAction))
    reason: Mapped[str] = mapped_column(String(500))
    proposed_content: Mapped[str] = mapped_column(Text)
    current_content: Mapped[str] = mapped_column(Text, default="")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.pending)
    reviewer: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    decision_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    instruction: Mapped[str] = mapped_column(Text)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.pending)
    plan: Mapped[list] = mapped_column(JSON, default=list)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    tool_calls: Mapped[list["ToolCallRecord"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    events: Mapped[list["AgentEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="AgentEvent.created_at"
    )
    artifacts: Mapped[list["AgentArtifact"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class ToolCallRecord(Base):
    __tablename__ = "tool_call_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(120))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="succeeded")
    risk_level: Mapped[str] = mapped_column(String(20), default="low")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    task: Mapped[AgentTask] = relationship(back_populates="tool_calls")


class AgentEvent(Base):
    __tablename__ = "agent_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    step_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    task: Mapped[AgentTask] = relationship(back_populates="events")


class AgentArtifact(Base):
    __tablename__ = "agent_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    tool_call_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("tool_call_records.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(300))
    artifact_type: Mapped[str] = mapped_column(String(60), default="markdown")
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    document_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("source_documents.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    task: Mapped[AgentTask] = relationship(back_populates="artifacts")


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    query: Mapped[str] = mapped_column(Text)
    expected_sources: Mapped[list] = mapped_column(JSON, default=list)
    expected_keywords: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(40), default="running")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    results: Mapped[list["EvaluationResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("evaluation_runs.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("evaluation_cases.id"), index=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    answer: Mapped[str] = mapped_column(Text, default="")
    trace: Mapped[dict] = mapped_column(JSON, default=dict)
    run: Mapped[EvaluationRun] = relationship(back_populates="results")


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(40), default="feishu", index=True)
    event_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    event_type: Mapped[str] = mapped_column(String(120), default="unknown")
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="received", index=True)
    agent_task_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_tasks.id"), nullable=True, index=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class QaRequirement(Base):
    __tablename__ = "qa_requirements"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(300), index=True)
    content: Mapped[str] = mapped_column(Text)
    source_document_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("source_documents.id"), nullable=True
    )
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    suites: Mapped[list["QaTestSuite"]] = relationship(
        back_populates="requirement", cascade="all, delete-orphan"
    )


class QaTestSuite(Base):
    __tablename__ = "qa_test_suites"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(ForeignKey("qa_requirements.id"), index=True)
    task_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_tasks.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(40), default="generated")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    testpoints: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    requirement: Mapped[QaRequirement] = relationship(back_populates="suites")
    cases: Mapped[list["QaTestCase"]] = relationship(
        back_populates="suite", cascade="all, delete-orphan"
    )


class QaTestCase(Base):
    __tablename__ = "qa_test_cases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    suite_id: Mapped[str] = mapped_column(ForeignKey("qa_test_suites.id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    precondition: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list] = mapped_column(JSON, default=list)
    expected_result: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(10), default="P2")
    status: Mapped[str] = mapped_column(String(40), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    suite: Mapped[QaTestSuite] = relationship(back_populates="cases")


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    fingerprint: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(60), default="webhook")
    title: Mapped[str] = mapped_column(String(300), index=True)
    service: Mapped[str] = mapped_column(String(200), default="unknown", index=True)
    severity: Mapped[str] = mapped_column(String(20), default="P3")
    status: Mapped[str] = mapped_column(String(40), default="firing", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    labels: Mapped[dict] = mapped_column(JSON, default=dict)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    triage: Mapped[dict] = mapped_column(JSON, default=dict)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    agent_task_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_tasks.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    incidents: Mapped[list["IncidentRecord"]] = relationship(back_populates="alert")


class IncidentRecord(Base):
    __tablename__ = "incident_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    alert_id: Mapped[str] = mapped_column(ForeignKey("alert_events.id"), index=True)
    task_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_tasks.id"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    severity: Mapped[str] = mapped_column(String(20), default="P3")
    status: Mapped[str] = mapped_column(String(40), default="open")
    summary: Mapped[str] = mapped_column(Text, default="")
    root_cause_hypothesis: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    recommended_actions: Mapped[list] = mapped_column(JSON, default=list)
    references: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    alert: Mapped[AlertEvent] = relationship(back_populates="incidents")
