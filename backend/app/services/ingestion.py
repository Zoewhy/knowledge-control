from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import DocumentFile, SourceClaim, SourceDocument, SourceSegment
from ..schemas import DocumentCreate
from .llm import llm
from .parsing import parse_document
from .parsing.types import ImageBlock, TableBlock, TextBlock
from .search_index import search_index
from .text import (
    SegmentDraft,
    count_tokens,
    fingerprint,
    split_markdown,
    split_text_block,
    table_to_markdown,
)

INGEST_STEPS = ("parsing", "chunking", "extracting", "indexing", "compiling")
_STEP_PROGRESS = {"parsing": 10, "chunking": 35, "extracting": 60, "indexing": 85, "compiling": 95}
PDF_MIN_TEXT_CHARS = 100


def _add_claims(db: Session, segment: SourceSegment) -> None:
    for item in llm.extract_claims(segment.content, segment.heading):
        statement = str(item.get("statement", "")).strip()
        if not statement:
            continue
        db.add(
            SourceClaim(
                segment_id=segment.id,
                statement=statement,
                scope=str(item.get("scope") or segment.heading or "general")[:200],
                confidence=float(item.get("confidence", 0.8)),
                fingerprint=fingerprint(statement),
            )
        )


def ingest_document(db: Session, payload: DocumentCreate) -> tuple[SourceDocument, bool]:
    checksum = fingerprint(payload.content)
    existing = db.scalar(select(SourceDocument).where(SourceDocument.checksum == checksum))
    if existing:
        return existing, False

    document = SourceDocument(
        title=payload.title,
        source_type=payload.source_type,
        source_uri=payload.source_uri,
        owner=payload.owner,
        checksum=checksum,
        status="processed",
        progress=100,
        metadata_json=payload.metadata,
    )
    db.add(document)
    db.flush()
    for ordinal, draft in enumerate(split_markdown(payload.content)):
        segment = SourceSegment(
            document_id=document.id,
            ordinal=ordinal,
            heading=draft.heading,
            heading_path=draft.heading_path,
            segment_type=draft.segment_type,
            content=draft.content,
            token_count=count_tokens(draft.content),
            embedding=llm.embed(draft.content),
        )
        db.add(segment)
        db.flush()
        search_index.index_segment(
            segment.id, document.id, document.title, segment.heading, segment.content
        )
        _add_claims(db, segment)
    db.commit()
    db.refresh(document)
    return document, True


def compile_document(db: Session, document: SourceDocument) -> dict[str, int]:
    from .compiler import compile_claims

    claim_ids = [claim.id for segment in document.segments for claim in segment.claims]
    return compile_claims(db, claim_ids).__dict__ if claim_ids else {}


def store_upload(filename: str, raw: bytes, checksum: str) -> str:
    """原文落盘：upload_dir/<sha256 前两位>/<sha256><原扩展名>，按内容去重。"""
    target_dir = Path(settings.upload_dir) / checksum[:2]
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{checksum}{Path(filename).suffix.lower()}"
    if not target.exists():
        target.write_bytes(raw)
    return str(target)


def _serialize_blocks(document: SourceDocument, raw: bytes, mime: str) -> list[dict]:
    parsed = parse_document(document.file.filename, raw, mime)
    if mime == "application/pdf":
        text_chars = parsed.meta.get("text_chars", 0)
        scanned_pages = parsed.meta.get("scanned_pages")
        if text_chars == 0 and not scanned_pages:
            raise ValueError("PDF 未提取到任何文本且非扫描件，解析疑似失败")
        if 0 < text_chars < PDF_MIN_TEXT_CHARS and not scanned_pages:
            warnings = list(document.metadata_json.get("parse_warnings", []))
            warnings.append(f"文本量较少（{text_chars} 字符），请确认原文是否完整")
            document.metadata_json = {**document.metadata_json, "parse_warnings": warnings}
    blocks: list[dict] = []
    for block in parsed.blocks:
        if isinstance(block, ImageBlock):
            try:
                description = llm.describe_image(block.image_bytes, hint=document.title)
            except Exception:
                description = ""
            block.caption = description.strip() or "[图片解析失败]"
            blocks.append(
                {
                    "type": "image",
                    "heading_path": block.heading_path,
                    "description": block.caption,
                }
            )
        elif isinstance(block, TableBlock):
            blocks.append(
                {
                    "type": "table",
                    "heading_path": block.heading_path,
                    "headers": block.headers,
                    "rows": block.rows,
                }
            )
        elif isinstance(block, TextBlock):
            blocks.append(
                {"type": "text", "heading_path": block.heading_path, "content": block.content}
            )
    document.metadata_json = {**document.metadata_json, "parse_meta": parsed.meta}
    return blocks


def _drafts_from_blocks(blocks: list[dict]) -> list[SegmentDraft]:
    drafts: list[SegmentDraft] = []
    for block in blocks:
        heading_path = list(block.get("heading_path") or [])
        heading = heading_path[-1] if heading_path else None
        if block["type"] == "table":
            content = table_to_markdown(block["headers"], block["rows"])
            drafts.append(
                SegmentDraft(
                    heading=heading,
                    heading_path=heading_path,
                    content=content,
                    segment_type="table",
                )
            )
        else:
            content = block.get("content") or block.get("description") or ""
            segment_type = "image" if block["type"] == "image" else "text"
            for draft in split_text_block(content, heading_path):
                draft.segment_type = segment_type
                drafts.append(draft)
    return [draft for draft in drafts if draft.content.strip()]


def _mark_step(db: Session, document: SourceDocument, step: str) -> None:
    document.status = step
    document.progress = _STEP_PROGRESS[step]
    db.commit()


def run_ingest_pipeline(db: Session, document_id: str) -> SourceDocument:
    """文件入库状态机：pending → parsing → … → compiling → processed。

    每一步幂等（解析结果落在 metadata_json、segment/claim/embedding 存在即跳过），
    失败置 failed + error_message，可通过 retry 从断点续跑。
    """
    document = db.get(SourceDocument, document_id)
    if document is None:
        raise ValueError(f"document not found: {document_id}")
    step = "parsing"
    try:
        document_file = document.file or (
            db.get(DocumentFile, document.file_id) if document.file_id else None
        )
        if document_file is None:
            raise ValueError("文档缺少原始文件记录")
        blocks = document.metadata_json.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            _mark_step(db, document, "parsing")
            raw = Path(document_file.storage_path).read_bytes()
            blocks = _serialize_blocks(document, raw, document_file.mime)
            document.metadata_json = {**document.metadata_json, "blocks": blocks}
            db.commit()

        step = "chunking"
        _mark_step(db, document, "chunking")
        segment_count = db.scalar(
            select(func.count())
            .select_from(SourceSegment)
            .where(SourceSegment.document_id == document.id)
        )
        if not segment_count:
            drafts = _drafts_from_blocks(blocks)
            if not drafts:
                raise ValueError("未解析出可用内容")
            for ordinal, draft in enumerate(drafts):
                db.add(
                    SourceSegment(
                        document_id=document.id,
                        ordinal=ordinal,
                        heading=draft.heading,
                        heading_path=draft.heading_path,
                        segment_type=draft.segment_type,
                        content=draft.content,
                        token_count=count_tokens(draft.content),
                    )
                )
            db.commit()
        segments = list(
            db.scalars(
                select(SourceSegment)
                .where(SourceSegment.document_id == document.id)
                .order_by(SourceSegment.ordinal)
            ).all()
        )

        step = "extracting"
        _mark_step(db, document, "extracting")
        for segment in segments:
            claim_count = db.scalar(
                select(func.count())
                .select_from(SourceClaim)
                .where(SourceClaim.segment_id == segment.id)
            )
            if claim_count:
                continue
            _add_claims(db, segment)
        db.commit()

        step = "indexing"
        _mark_step(db, document, "indexing")
        for segment in segments:
            if segment.embedding:
                continue
            segment.embedding = llm.embed(segment.content)
            db.flush()
            search_index.index_segment(
                segment.id, document.id, document.title, segment.heading, segment.content
            )
        db.commit()

        if document.metadata_json.get("compile", True):
            step = "compiling"
            _mark_step(db, document, "compiling")
            db.refresh(document)
            compile_document(db, document)

        document.status = "processed"
        document.progress = 100
        document.error_message = None
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = db.get(SourceDocument, document_id)
        if failed is not None:
            failed.status = "failed"
            failed.error_message = f"{step}: {exc}"[:500]
            db.commit()
            document = failed
    return document


def dispatch_ingest(db: Session, document: SourceDocument) -> None:
    if settings.ingest_execution_mode == "sync":
        run_ingest_pipeline(db, document.id)
        return
    if settings.ingest_execution_mode != "celery":
        raise ValueError(f"未知入库执行模式: {settings.ingest_execution_mode}")
    from ..worker import execute_ingest_task

    execute_ingest_task.delay(document.id)
