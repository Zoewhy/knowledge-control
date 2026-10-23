from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .database import get_db
from .models import (
    AgentArtifact,
    AgentEvent,
    AgentTask,
    EvaluationCase,
    EvaluationRun,
    ReviewStatus,
    ReviewTask,
    SourceClaim,
    SourceDocument,
    SourceSegment,
    TaskStatus,
    WikiCitation,
    WikiPage,
    WikiSection,
)
from .schemas import (
    AgentTaskCreate,
    AgentTaskRead,
    DashboardStats,
    DocumentBatchCreate,
    DocumentCreate,
    DocumentRead,
    EvaluationCaseCreate,
    EvaluationRunRead,
    FeishuDocumentImport,
    ReviewDecision,
    ReviewTaskRead,
    SearchRequest,
    SearchResponse,
    WikiPageRead,
)
from .services.agent_runtime import create_task, registry
from .services.compiler import compile_claims, decide_review
from .services.evaluation import run_evaluation
from .services.feishu import FeishuError, feishu_client
from .services.ingestion import compile_document, ingest_document
from .services.retrieval import hybrid_search
from .services.scheduler import dispatch_task

router = APIRouter()


def _document_query(document_id: str):
    return (
        select(SourceDocument)
        .where(SourceDocument.id == document_id)
        .options(selectinload(SourceDocument.segments).selectinload(SourceSegment.claims))
    )


def _task_options():
    return (
        selectinload(AgentTask.tool_calls),
        selectinload(AgentTask.events),
        selectinload(AgentTask.artifacts),
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "alg-qa-agent"}


@router.get("/dashboard", response_model=DashboardStats)
def dashboard(db: Session = Depends(get_db)) -> DashboardStats:
    def count(model, *conditions) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0

    return DashboardStats(
        documents=count(SourceDocument),
        segments=count(SourceSegment),
        claims=count(SourceClaim),
        wiki_pages=count(WikiPage),
        pending_reviews=count(ReviewTask, ReviewTask.status == ReviewStatus.pending),
        agent_tasks=count(AgentTask),
        evaluation_runs=count(EvaluationRun),
    )


@router.post("/documents", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
def create_document(payload: DocumentCreate, db: Session = Depends(get_db)):
    document, created = ingest_document(db, payload)
    if payload.compile and created:
        compile_document(db, document)
    return db.scalar(_document_query(document.id))


@router.post("/documents/upload", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    owner: str = Form("unknown"),
    compile: bool = Form(True),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith((".md", ".markdown", ".txt")):
        raise HTTPException(415, "仅支持 Markdown 或 UTF-8 文本文件")
    raw = await file.read(5 * 1024 * 1024 + 1)
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 5 MB")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(415, "文件必须使用 UTF-8 编码") from exc
    if not content.strip():
        raise HTTPException(422, "文件内容不能为空")
    payload = DocumentCreate(
        title=title or file.filename,
        content=content,
        source_type="file_upload",
        owner=owner,
        metadata={"filename": file.filename, "content_type": file.content_type},
        compile=compile,
    )
    document, created = ingest_document(db, payload)
    if compile and created:
        compile_document(db, document)
    return db.scalar(_document_query(document.id))


@router.post("/documents/batch", status_code=status.HTTP_201_CREATED)
def create_document_batch(payload: DocumentBatchCreate, db: Session = Depends(get_db)) -> dict:
    results = []
    for item in payload.documents:
        document, created = ingest_document(db, item)
        compile_result = compile_document(db, document) if item.compile and created else {}
        results.append(
            {
                "document_id": document.id,
                "created": created,
                "title": document.title,
                "compile": compile_result,
            }
        )
    return {"results": results, "total": len(results)}


@router.post("/documents/feishu", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
def import_feishu_document(payload: FeishuDocumentImport, db: Session = Depends(get_db)):
    try:
        content = feishu_client.read_document(payload.document_token)
    except FeishuError as exc:
        raise HTTPException(502, str(exc)) from exc
    document_payload = DocumentCreate(
        title=payload.title or f"飞书文档 {payload.document_token}",
        content=content,
        source_type="feishu",
        source_uri=f"feishu://docx/{payload.document_token}",
        owner=payload.owner,
        metadata={"document_token": payload.document_token},
        compile=payload.compile,
    )
    document, created = ingest_document(db, document_payload)
    if payload.compile and created:
        compile_document(db, document)
    return db.scalar(_document_query(document.id))


@router.get("/documents", response_model=list[DocumentRead])
def list_documents(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(SourceDocument)
            .order_by(SourceDocument.created_at.desc())
            .limit(limit)
            .options(selectinload(SourceDocument.segments).selectinload(SourceSegment.claims))
        ).all()
    )


@router.post("/claims/{claim_id}/compile")
def compile_one(claim_id: str, db: Session = Depends(get_db)) -> dict:
    if not db.get(SourceClaim, claim_id):
        raise HTTPException(404, "Claim not found")
    return compile_claims(db, [claim_id]).__dict__


def wiki_query():
    return select(WikiPage).options(
        selectinload(WikiPage.sections)
        .selectinload(WikiSection.citations)
        .selectinload(WikiCitation.claim)
        .selectinload(SourceClaim.segment)
        .selectinload(SourceSegment.document)
    )


def serialize_wiki(page: WikiPage) -> dict:
    return {
        "id": page.id,
        "slug": page.slug,
        "title": page.title,
        "summary": page.summary,
        "owner": page.owner,
        "version": page.version,
        "updated_at": page.updated_at,
        "sections": [
            {
                "id": section.id,
                "heading": section.heading,
                "content": section.content,
                "ordinal": section.ordinal,
                "citations": [
                    {
                        "claim_id": citation.claim.id,
                        "statement": citation.claim.statement,
                        "document_id": citation.claim.segment.document.id,
                        "document_title": citation.claim.segment.document.title,
                        "segment_id": citation.claim.segment.id,
                        "source_uri": citation.claim.segment.document.source_uri,
                    }
                    for citation in section.citations
                ],
            }
            for section in page.sections
        ],
    }


@router.get("/wiki", response_model=list[WikiPageRead])
def list_wiki(db: Session = Depends(get_db)):
    pages = list(db.scalars(wiki_query().order_by(WikiPage.updated_at.desc())).unique().all())
    return [serialize_wiki(page) for page in pages]


@router.get("/wiki/{page_id}", response_model=WikiPageRead)
def get_wiki(page_id: str, db: Session = Depends(get_db)):
    page = db.scalar(wiki_query().where(WikiPage.id == page_id))
    if not page:
        raise HTTPException(404, "Wiki page not found")
    return serialize_wiki(page)


def serialize_review(db: Session, task: ReviewTask) -> dict:
    claim = db.scalar(
        select(SourceClaim)
        .where(SourceClaim.id == task.claim_id)
        .options(selectinload(SourceClaim.segment).selectinload(SourceSegment.document))
    )
    section = db.get(WikiSection, task.target_section_id) if task.target_section_id else None
    page = db.get(WikiPage, task.target_page_id) if task.target_page_id else None
    return {
        **{column.name: getattr(task, column.name) for column in ReviewTask.__table__.columns},
        "source_context": {
            "statement": claim.statement,
            "scope": claim.scope,
            "confidence": claim.confidence,
            "segment_id": claim.segment_id,
            "segment_content": claim.segment.content,
            "document_id": claim.segment.document.id,
            "document_title": claim.segment.document.title,
            "source_uri": claim.segment.document.source_uri,
        },
        "target_wiki": {
            "page_title": page.title if page else None,
            "section_heading": section.heading if section else None,
        },
    }


@router.get("/reviews", response_model=list[ReviewTaskRead])
def list_reviews(
    review_status: Optional[ReviewStatus] = Query(None, alias="status"),
    db: Session = Depends(get_db),
):
    query = select(ReviewTask).order_by(ReviewTask.created_at.desc())
    if review_status:
        query = query.where(ReviewTask.status == review_status)
    return [serialize_review(db, task) for task in db.scalars(query).all()]


@router.post("/reviews/{task_id}/decision", response_model=ReviewTaskRead)
def review_decision(task_id: str, payload: ReviewDecision, db: Session = Depends(get_db)):
    task = db.get(ReviewTask, task_id)
    if not task:
        raise HTTPException(404, "Review task not found")
    try:
        decide_review(
            db,
            task,
            payload.decision,
            payload.reviewer,
            payload.note,
            scope=payload.scope,
            owner=payload.owner,
            target_section_id=payload.target_section_id,
            proposed_content=payload.proposed_content,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    db.refresh(task)
    return serialize_review(db, task)


@router.post("/search", response_model=SearchResponse)
def search(payload: SearchRequest, db: Session = Depends(get_db)):
    return hybrid_search(db, payload.query, payload.top_k, payload.include_answer)


@router.get("/tools")
def tools() -> list[dict[str, Any]]:
    return registry.list()


@router.post("/agent/tasks", response_model=AgentTaskRead, status_code=status.HTTP_201_CREATED)
def add_agent_task(payload: AgentTaskCreate, db: Session = Depends(get_db)):
    task = create_task(db, payload.instruction, payload.context)
    if payload.auto_run:
        task = dispatch_task(db, task)
    return db.scalar(select(AgentTask).where(AgentTask.id == task.id).options(*_task_options()))


@router.get("/agent/tasks", response_model=list[AgentTaskRead])
def list_agent_tasks(db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(AgentTask).order_by(AgentTask.created_at.desc()).options(*_task_options())
        ).all()
    )


@router.post("/agent/tasks/{task_id}/approve", response_model=AgentTaskRead)
def approve_agent_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(404, "Agent task not found")
    if task.status != TaskStatus.waiting_approval:
        raise HTTPException(409, "任务当前不在待审批状态")
    task = dispatch_task(db, task, approve_medium_risk=True)
    return db.scalar(select(AgentTask).where(AgentTask.id == task.id).options(*_task_options()))


@router.post("/agent/tasks/{task_id}/resume", response_model=AgentTaskRead)
def resume_agent_task(task_id: str, db: Session = Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if not task:
        raise HTTPException(404, "Agent task not found")
    if task.status not in {TaskStatus.failed, TaskStatus.running, TaskStatus.pending}:
        raise HTTPException(409, "只有失败、中断或待执行任务可以恢复")
    task = dispatch_task(db, task)
    return db.scalar(select(AgentTask).where(AgentTask.id == task.id).options(*_task_options()))


@router.post("/agent/tasks/{task_id}/replay", response_model=AgentTaskRead)
def replay_agent_task(task_id: str, db: Session = Depends(get_db)):
    original = db.get(AgentTask, task_id)
    if not original:
        raise HTTPException(404, "Agent task not found")
    task = create_task(db, original.instruction, {**original.context, "replay_of": original.id})
    task = dispatch_task(db, task)
    return db.scalar(select(AgentTask).where(AgentTask.id == task.id).options(*_task_options()))


@router.post("/agent/artifacts/{artifact_id}/reflow", response_model=DocumentRead)
def reflow_agent_artifact(artifact_id: str, db: Session = Depends(get_db)):
    artifact = db.get(AgentArtifact, artifact_id)
    if not artifact:
        raise HTTPException(404, "Agent artifact not found")
    if artifact.document_id:
        return db.scalar(_document_query(artifact.document_id))
    payload = DocumentCreate(
        title=artifact.name,
        content=artifact.content,
        source_type="agent_artifact",
        source_uri=f"agent://artifact/{artifact.id}",
        owner="Agent Runtime",
        metadata={
            "task_id": artifact.task_id,
            "artifact_id": artifact.id,
            **artifact.metadata_json,
        },
    )
    document, created = ingest_document(db, payload)
    if created:
        compile_document(db, document)
    artifact.document_id = document.id
    db.add(
        AgentEvent(
            task_id=artifact.task_id,
            event_type="artifact_reflowed",
            payload={"document_id": document.id, "artifact_id": artifact.id},
        )
    )
    db.commit()
    return db.scalar(_document_query(document.id))


@router.post("/evaluations/cases", status_code=status.HTTP_201_CREATED)
def add_evaluation_case(payload: EvaluationCaseCreate, db: Session = Depends(get_db)):
    case = EvaluationCase(**payload.model_dump())
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.post("/evaluations/runs", response_model=EvaluationRunRead)
def add_evaluation_run(db: Session = Depends(get_db)):
    run = run_evaluation(db)
    return db.scalar(
        select(EvaluationRun)
        .where(EvaluationRun.id == run.id)
        .options(selectinload(EvaluationRun.results))
    )


@router.get("/evaluations/runs", response_model=list[EvaluationRunRead])
def list_evaluation_runs(db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(EvaluationRun)
            .order_by(EvaluationRun.created_at.desc())
            .options(selectinload(EvaluationRun.results))
        ).all()
    )
