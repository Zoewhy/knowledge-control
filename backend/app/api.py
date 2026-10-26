from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import filetype
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .config import settings
from .database import get_db
from .models import (
    AgentArtifact,
    AgentEvent,
    AgentTask,
    AlertEvent,
    DocumentFile,
    EvaluationCase,
    EvaluationRun,
    IncidentRecord,
    IntegrationEvent,
    QaRequirement,
    QaTestSuite,
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
    AlertEventRead,
    AlertWebhook,
    DashboardStats,
    DocumentBatchCreate,
    DocumentCreate,
    DocumentRead,
    DocumentStatusRead,
    EvaluationArmSummary,
    EvaluationCaseCreate,
    EvaluationCompareResponse,
    EvaluationRunRead,
    EvaluationRunRequest,
    FeishuDocumentImport,
    IncidentRecordRead,
    IntegrationEventRead,
    QaRequirementCreate,
    QaRequirementRead,
    QaTestSuiteRead,
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
from .services.ingestion import (
    compile_document,
    dispatch_ingest,
    ingest_document,
    store_upload,
)
from .services.integrations import (
    IntegrationError,
    create_alert_triage_task,
    handle_feishu_event,
    ingest_alert,
)
from .services.qa_tools import build_suite_report
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


ALLOWED_UPLOAD_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _sniff_upload_mime(filename: str, raw: bytes) -> str:
    expected = ALLOWED_UPLOAD_TYPES[Path(filename).suffix.lower()]
    detected = filetype.guess(raw)
    if detected is None:
        if expected in ("text/markdown", "text/plain"):
            try:
                raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise HTTPException(422, "文本文件必须使用 UTF-8 编码") from exc
            return expected
        raise HTTPException(422, "无法识别文件真实类型")
    if detected.mime != expected:
        raise HTTPException(422, f"文件内容与扩展名不符：声明 {expected}，实际 {detected.mime}")
    return expected


@router.post("/documents/upload", response_model=DocumentRead, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    owner: str = Form("unknown"),
    compile: bool = Form(True),
    db: Session = Depends(get_db),
):
    if not file.filename or Path(file.filename).suffix.lower() not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(415, "仅支持 Markdown / TXT / PDF / DOCX / XLSX / PNG / JPG 文件")
    max_bytes = settings.upload_max_mb * 1024 * 1024
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(413, f"文件不能超过 {settings.upload_max_mb} MB")
    if not raw:
        raise HTTPException(422, "文件内容不能为空")
    mime = _sniff_upload_mime(file.filename, raw)
    checksum = hashlib.sha256(raw).hexdigest()
    existing = db.scalar(select(SourceDocument).where(SourceDocument.checksum == checksum))
    if existing:
        return db.scalar(_document_query(existing.id))
    storage_path = store_upload(file.filename, raw, checksum)
    document = SourceDocument(
        title=title or file.filename,
        source_type="file_upload",
        owner=owner,
        checksum=checksum,
        status="pending",
        progress=0,
        metadata_json={"filename": file.filename, "mime": mime, "compile": compile},
    )
    db.add(document)
    db.flush()
    document_file = DocumentFile(
        document_id=document.id,
        filename=file.filename,
        mime=mime,
        size_bytes=len(raw),
        sha256=checksum,
        storage_path=storage_path,
    )
    db.add(document_file)
    db.flush()
    document.file_id = document_file.id
    db.commit()
    dispatch_ingest(db, document)
    return db.scalar(_document_query(document.id))


@router.get("/documents/{document_id}/status", response_model=DocumentStatusRead)
def get_document_status(document_id: str, db: Session = Depends(get_db)):
    document = db.get(SourceDocument, document_id)
    if not document:
        raise HTTPException(404, "Document not found")
    return document


@router.post("/documents/{document_id}/retry", response_model=DocumentStatusRead)
def retry_document(document_id: str, db: Session = Depends(get_db)):
    document = db.get(SourceDocument, document_id)
    if not document:
        raise HTTPException(404, "Document not found")
    if document.status != "failed":
        raise HTTPException(409, "只有失败的文档可以重试")
    document.status = "pending"
    document.progress = 0
    document.error_message = None
    db.commit()
    dispatch_ingest(db, document)
    db.refresh(document)
    return document


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
def add_evaluation_run(
    payload: Optional[EvaluationRunRequest] = None, db: Session = Depends(get_db)
):
    arm = payload.arm if payload else "G3"
    run = run_evaluation(db, arm=arm)
    return db.scalar(
        select(EvaluationRun)
        .where(EvaluationRun.id == run.id)
        .options(selectinload(EvaluationRun.results))
    )


@router.get("/evaluations/compare", response_model=EvaluationCompareResponse)
def compare_evaluations(db: Session = Depends(get_db)):
    runs = db.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc())).all()
    arms: dict[str, EvaluationArmSummary] = {}
    for run in runs:
        arm = (run.metrics or {}).get("arm")
        if not arm or arm in arms:
            continue
        overall = {
            key: value for key, value in run.metrics.items() if key not in {"arm", "by_type"}
        }
        arms[arm] = EvaluationArmSummary(
            run_id=run.id,
            created_at=run.created_at,
            overall=overall,
            by_type=run.metrics.get("by_type", {}),
        )
    return EvaluationCompareResponse(arms=arms)


@router.get("/evaluations/runs", response_model=list[EvaluationRunRead])
def list_evaluation_runs(db: Session = Depends(get_db)):
    return list(
        db.scalars(
            select(EvaluationRun)
            .order_by(EvaluationRun.created_at.desc())
            .options(selectinload(EvaluationRun.results))
        ).all()
    )


# ---------- Integration Gateway ----------


@router.post("/integrations/feishu/events")
async def feishu_events(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if len(raw) > settings.integration_payload_max_bytes:
        raise HTTPException(413, "事件 payload 超过大小限制")
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "请求体必须是合法 JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    try:
        return handle_feishu_event(db, body)
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/integrations/events", response_model=list[IntegrationEventRead])
def list_integration_events(
    provider: Optional[str] = Query(None),
    event_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = select(IntegrationEvent).order_by(IntegrationEvent.created_at.desc()).limit(limit)
    if provider:
        query = query.where(IntegrationEvent.provider == provider)
    if event_status:
        query = query.where(IntegrationEvent.status == event_status)
    return list(db.scalars(query).all())


@router.get("/integrations/events/{event_id}")
def get_integration_event(event_id: str, db: Session = Depends(get_db)):
    event = db.get(IntegrationEvent, event_id)
    if not event:
        raise HTTPException(404, "Integration event not found")
    return IntegrationEventRead.model_validate(event).model_dump(mode="json") | {
        "payload": event.payload
    }


@router.post("/integrations/alerts", status_code=status.HTTP_201_CREATED)
def receive_alert(payload: AlertWebhook, db: Session = Depends(get_db)):
    alert, created, task_id = ingest_alert(
        db,
        title=payload.title,
        source=payload.source,
        service=payload.service,
        severity=payload.severity,
        description=payload.description,
        labels=payload.labels,
        fingerprint=payload.fingerprint,
        auto_triage=payload.auto_triage,
    )
    return {
        "alert": AlertEventRead.model_validate(alert).model_dump(mode="json"),
        "created": created,
        "agent_task_id": task_id,
    }


# ---------- 告警归因 ----------


@router.get("/alerts", response_model=list[AlertEventRead])
def list_alerts(
    alert_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = select(AlertEvent).order_by(AlertEvent.created_at.desc()).limit(limit)
    if alert_status:
        query = query.where(AlertEvent.status == alert_status)
    return list(db.scalars(query).all())


@router.get("/alerts/{alert_id}", response_model=AlertEventRead)
def get_alert(alert_id: str, db: Session = Depends(get_db)):
    alert = db.get(AlertEvent, alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found")
    return alert


@router.post("/alerts/{alert_id}/triage", status_code=status.HTTP_201_CREATED)
def triage_alert(alert_id: str, db: Session = Depends(get_db)):
    alert = db.get(AlertEvent, alert_id)
    if not alert:
        raise HTTPException(404, "Alert not found")
    if alert.agent_task_id:
        return {"alert_id": alert.id, "agent_task_id": alert.agent_task_id, "reused": True}
    task_id = create_alert_triage_task(db, alert)
    return {"alert_id": alert.id, "agent_task_id": task_id, "reused": False}


@router.get("/incidents", response_model=list[IncidentRecordRead])
def list_incidents(db: Session = Depends(get_db)):
    return list(
        db.scalars(select(IncidentRecord).order_by(IncidentRecord.created_at.desc())).all()
    )


@router.get("/incidents/{incident_id}", response_model=IncidentRecordRead)
def get_incident(incident_id: str, db: Session = Depends(get_db)):
    incident = db.get(IncidentRecord, incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    return incident


# ---------- QA 测试闭环 ----------


def _suite_query():
    return select(QaTestSuite).options(selectinload(QaTestSuite.cases))


@router.post("/qa/requirements", status_code=status.HTTP_201_CREATED)
def create_qa_requirement(payload: QaRequirementCreate, db: Session = Depends(get_db)):
    requirement = QaRequirement(
        title=payload.title,
        content=payload.content,
        source_document_id=payload.source_document_id,
        metadata_json=payload.metadata,
    )
    db.add(requirement)
    db.commit()
    db.refresh(requirement)
    task_id = None
    if payload.auto_run:
        task = create_task(
            db,
            f"为「{requirement.title}」生成测试用例",
            {
                "task_type": "qa_generate",
                "requirement_id": requirement.id,
                "query": requirement.title,
            },
        )
        task = dispatch_task(db, task)
        task_id = task.id
    return {
        "requirement": QaRequirementRead.model_validate(requirement).model_dump(mode="json"),
        "agent_task_id": task_id,
    }


@router.post("/qa/requirements/{requirement_id}/generate", status_code=status.HTTP_201_CREATED)
def generate_qa_cases(requirement_id: str, db: Session = Depends(get_db)):
    requirement = db.get(QaRequirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Requirement not found")
    task = create_task(
        db,
        f"为「{requirement.title}」生成测试用例",
        {
            "task_type": "qa_generate",
            "requirement_id": requirement.id,
            "query": requirement.title,
        },
    )
    task = dispatch_task(db, task)
    return {
        "agent_task_id": task.id,
        "task_status": task.status.value,
        "requirement_id": requirement.id,
    }


@router.get("/qa/requirements", response_model=list[QaRequirementRead])
def list_qa_requirements(db: Session = Depends(get_db)):
    return list(
        db.scalars(select(QaRequirement).order_by(QaRequirement.created_at.desc())).all()
    )


@router.get("/qa/test-suites", response_model=list[QaTestSuiteRead])
def list_qa_suites(
    requirement_id: Optional[str] = Query(None), db: Session = Depends(get_db)
):
    query = _suite_query().order_by(QaTestSuite.created_at.desc())
    if requirement_id:
        query = query.where(QaTestSuite.requirement_id == requirement_id)
    return list(db.scalars(query).unique().all())


@router.get("/qa/test-suites/{suite_id}", response_model=QaTestSuiteRead)
def get_qa_suite(suite_id: str, db: Session = Depends(get_db)):
    suite = db.scalar(_suite_query().where(QaTestSuite.id == suite_id))
    if not suite:
        raise HTTPException(404, "Test suite not found")
    return suite


@router.post("/qa/test-suites/{suite_id}/report")
def qa_suite_report(suite_id: str, db: Session = Depends(get_db)):
    suite = db.scalar(_suite_query().where(QaTestSuite.id == suite_id))
    if not suite:
        raise HTTPException(404, "Test suite not found")
    markdown = build_suite_report(db, suite)
    artifact_id = None
    if suite.task_id:
        existing = db.scalar(
            select(AgentArtifact).where(
                AgentArtifact.task_id == suite.task_id,
                AgentArtifact.name == f"测试报告-{suite.id}",
            )
        )
        if existing:
            existing.content = markdown
            artifact_id = existing.id
        else:
            artifact = AgentArtifact(
                task_id=suite.task_id,
                name=f"测试报告-{suite.id}",
                artifact_type="markdown",
                content=markdown,
                metadata_json={"suite_id": suite.id, "source": "qa_suite_report"},
            )
            db.add(artifact)
            db.flush()
            artifact_id = artifact.id
        db.commit()
    return {"suite_id": suite.id, "markdown": markdown, "artifact_id": artifact_id}


@router.post("/qa/test-suites/{suite_id}/reflow", response_model=DocumentRead)
def qa_suite_reflow(suite_id: str, db: Session = Depends(get_db)):
    suite = db.scalar(_suite_query().where(QaTestSuite.id == suite_id))
    if not suite:
        raise HTTPException(404, "Test suite not found")
    markdown = build_suite_report(db, suite)
    document, created = ingest_document(
        db,
        DocumentCreate(
            title=f"{suite.name} 测试报告",
            content=markdown,
            source_type="qa_report",
            source_uri=f"qa://suite/{suite.id}",
            owner="QA Agent",
            metadata={"suite_id": suite.id, "requirement_id": suite.requirement_id},
        ),
    )
    if created:
        compile_document(db, document)
    return db.scalar(_document_query(document.id))
