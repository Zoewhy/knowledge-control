from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import DocumentFile, IngestPolicy, SourceClaim, SourceDocument, SourceSegment
from ..schemas import DocumentCreate
from .chunkers import get_chunker
from .graph import extract_dependencies_to_graph
from .llm import llm
from .parsing import parse_document
from .parsing.types import ImageBlock, TableBlock, TextBlock
from .policies import resolve_policy
from .search_index import search_index
from .text import (
    SegmentDraft,
    count_tokens,
    fingerprint,
    split_markdown,
)

INGEST_STEPS = ("parsing", "chunking", "extracting", "indexing", "compiling")
_STEP_PROGRESS = {"parsing": 10, "chunking": 35, "extracting": 60, "indexing": 85, "compiling": 95}
PDF_MIN_TEXT_CHARS = 100

logger = logging.getLogger(__name__)


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


def _index_claim_dependencies(db: Session, claims: list[SourceClaim]) -> None:
    """从 claim 文本抽取服务依赖边；失败只记 warning，不阻断入库管线。"""
    for claim in claims:
        try:
            extract_dependencies_to_graph(db, claim.statement, claim.id)
        except Exception as exc:
            logger.warning("dependency extraction failed for claim %s: %s", claim.id, exc)


def _chunk_raw_content(content: str, policy: IngestPolicy) -> list[SegmentDraft]:
    """同步入库（无文件块）按策略切分；heading 走 split_markdown 保持现状行为。"""
    params = dict(policy.chunk_params or {})
    if policy.chunk_strategy == "heading":
        return split_markdown(
            content,
            max_tokens=int(params.get("max_tokens", 512)),
            overlap_tokens=int(params.get("overlap_tokens", 50)),
        )
    blocks = [{"type": "text", "heading_path": [], "content": content}]
    return get_chunker(policy.chunk_strategy)(blocks, params)


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
    policy = resolve_policy(db, document)
    for ordinal, draft in enumerate(_chunk_raw_content(payload.content, policy)):
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
        db.flush()
        _index_claim_dependencies(db, list(segment.claims))
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


def build_blocks(
    filename: str,
    raw: bytes,
    mime: str,
    *,
    title: str,
    parser_strategy: str = "auto",
    min_text_chars: int = PDF_MIN_TEXT_CHARS,
) -> tuple[list[dict], dict, list[str]]:
    """解析原文为序列化块；text_only 策略跳过图片 VL 描述直接记占位。"""
    parsed = parse_document(filename, raw, mime)
    warnings: list[str] = []
    if mime == "application/pdf":
        text_chars = parsed.meta.get("text_chars", 0)
        scanned_pages = parsed.meta.get("scanned_pages")
        if text_chars == 0 and not scanned_pages:
            raise ValueError("PDF 未提取到任何文本且非扫描件，解析疑似失败")
        if 0 < text_chars < min_text_chars and not scanned_pages:
            warnings.append(f"文本量较少（{text_chars} 字符），请确认原文是否完整")
    blocks: list[dict] = []
    for block in parsed.blocks:
        if isinstance(block, ImageBlock):
            if parser_strategy == "text_only":
                caption = "[图片已跳过]"
            else:
                try:
                    caption = llm.describe_image(block.image_bytes, hint=title).strip()
                except Exception:
                    caption = ""
                caption = caption or "[图片解析失败]"
            blocks.append(
                {
                    "type": "image",
                    "heading_path": block.heading_path,
                    "description": caption,
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
    return blocks, parsed.meta, warnings


def _serialize_blocks(
    document: SourceDocument, raw: bytes, mime: str, policy: IngestPolicy
) -> list[dict]:
    filename = document.file.filename if document.file else "document.md"
    blocks, meta, warnings = build_blocks(
        filename,
        raw,
        mime,
        title=document.title,
        parser_strategy=policy.parser_strategy,
        min_text_chars=policy.min_text_chars,
    )
    metadata = {**document.metadata_json, "parse_meta": meta}
    if warnings:
        metadata["parse_warnings"] = [
            *document.metadata_json.get("parse_warnings", []),
            *warnings,
        ]
    document.metadata_json = metadata
    return blocks


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
        policy = resolve_policy(db, document)
        document_file = document.file or (
            db.get(DocumentFile, document.file_id) if document.file_id else None
        )
        if document_file is None:
            raise ValueError("文档缺少原始文件记录")
        blocks = document.metadata_json.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            _mark_step(db, document, "parsing")
            raw = Path(document_file.storage_path).read_bytes()
            blocks = _serialize_blocks(document, raw, document_file.mime, policy)
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
            chunker = get_chunker(policy.chunk_strategy)
            drafts = chunker(blocks, dict(policy.chunk_params or {}))
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
        newly_indexed: list[SourceSegment] = []
        for segment in segments:
            if segment.embedding:
                continue
            segment.embedding = llm.embed(segment.content)
            db.flush()
            search_index.index_segment(
                segment.id, document.id, document.title, segment.heading, segment.content
            )
            newly_indexed.append(segment)
        if newly_indexed:
            new_claims = list(
                db.scalars(
                    select(SourceClaim).where(
                        SourceClaim.segment_id.in_([segment.id for segment in newly_indexed])
                    )
                ).all()
            )
            _index_claim_dependencies(db, new_claims)
        db.commit()

        if policy.auto_compile and document.metadata_json.get("compile", True):
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
