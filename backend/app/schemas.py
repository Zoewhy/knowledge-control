from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import PatchAction, ReviewStatus, TaskStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1)
    source_type: str = "manual"
    source_uri: Optional[str] = None
    owner: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)
    compile: bool = True


class DocumentBatchCreate(BaseModel):
    documents: list[DocumentCreate] = Field(min_length=1, max_length=200)


class FeishuDocumentImport(BaseModel):
    document_token: str = Field(min_length=1)
    title: Optional[str] = Field(default=None, max_length=300)
    owner: str = "unknown"
    compile: bool = True


class ClaimRead(ORMModel):
    id: str
    statement: str
    scope: str
    confidence: float
    status: str


class SegmentRead(ORMModel):
    id: str
    ordinal: int
    heading: Optional[str]
    content: str
    claims: list[ClaimRead] = Field(default_factory=list)


class DocumentRead(ORMModel):
    id: str
    title: str
    source_type: str
    source_uri: Optional[str]
    owner: str
    status: str
    created_at: datetime
    segments: list[SegmentRead] = Field(default_factory=list)


class CitationRead(BaseModel):
    claim_id: str
    statement: str
    document_id: str
    document_title: str
    segment_id: str
    source_uri: Optional[str] = None


class SectionRead(ORMModel):
    id: str
    heading: str
    content: str
    ordinal: int
    citations: list[CitationRead] = Field(default_factory=list)


class WikiPageRead(ORMModel):
    id: str
    slug: str
    title: str
    summary: str
    owner: str
    version: int
    updated_at: datetime
    sections: list[SectionRead] = Field(default_factory=list)


class ReviewTaskRead(ORMModel):
    id: str
    claim_id: str
    target_page_id: Optional[str]
    target_section_id: Optional[str]
    action: PatchAction
    reason: str
    proposed_content: str
    current_content: str
    risk_score: float
    status: ReviewStatus
    reviewer: Optional[str]
    decision_note: Optional[str]
    created_at: datetime
    source_context: dict[str, Any] = Field(default_factory=dict)
    target_wiki: dict[str, Any] = Field(default_factory=dict)


class ReviewDecision(BaseModel):
    decision: Literal["approve", "reject", "keep_current", "source_only"]
    reviewer: str = Field(min_length=1)
    note: str = ""
    scope: Optional[str] = Field(default=None, min_length=1, max_length=200)
    owner: Optional[str] = Field(default=None, min_length=1, max_length=120)
    target_section_id: Optional[str] = None
    proposed_content: Optional[str] = Field(default=None, min_length=1)


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=50)
    include_answer: bool = True


class SearchHit(BaseModel):
    channel: str
    score: float
    page_id: Optional[str] = None
    section_id: Optional[str] = None
    segment_id: Optional[str] = None
    title: str
    content: str
    source: Optional[CitationRead] = None
    citations: list[CitationRead] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    answer: Optional[str]
    hits: list[SearchHit]
    diagnostics: dict[str, Any]
    latency_ms: float


class AgentTaskCreate(BaseModel):
    instruction: str = Field(min_length=2)
    context: dict[str, Any] = Field(default_factory=dict)
    auto_run: bool = True


class ToolCallRead(ORMModel):
    id: str
    step_index: int
    tool_name: str
    arguments: dict[str, Any]
    output: dict[str, Any]
    status: str
    risk_level: str


class AgentEventRead(ORMModel):
    id: str
    step_index: Optional[int]
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class AgentArtifactRead(ORMModel):
    id: str
    name: str
    artifact_type: str
    content: str
    metadata_json: dict[str, Any]
    document_id: Optional[str]
    created_at: datetime


class AgentTaskRead(ORMModel):
    id: str
    instruction: str
    status: TaskStatus
    plan: list[dict[str, Any]]
    step_index: int
    context: dict[str, Any]
    result: dict[str, Any]
    error: Optional[str]
    error_detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    tool_calls: list[ToolCallRead] = Field(default_factory=list)
    events: list[AgentEventRead] = Field(default_factory=list)
    artifacts: list[AgentArtifactRead] = Field(default_factory=list)


class EvaluationCaseCreate(BaseModel):
    name: str
    query: str
    expected_sources: list[str] = Field(default_factory=list)
    expected_keywords: list[str] = Field(default_factory=list)


class EvaluationResultRead(ORMModel):
    id: str
    case_id: str
    metrics: dict[str, Any]
    answer: str
    trace: dict[str, Any]


class EvaluationRunRead(ORMModel):
    id: str
    status: str
    metrics: dict[str, Any]
    created_at: datetime
    finished_at: Optional[datetime]
    results: list[EvaluationResultRead] = Field(default_factory=list)


class DashboardStats(BaseModel):
    documents: int
    segments: int
    claims: int
    wiki_pages: int
    pending_reviews: int
    agent_tasks: int
    evaluation_runs: int
