"""重处理引擎：从 DocumentFile 原文按策略快照重建实验副本。

claim/embedding 缓存：段内容 fingerprint 命中库内已有段的抽取结果时直接复制，
跳过 LLM 抽取（内容相同则抽取结果确定相同），重切分 ≠ 重抽取。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    DocumentFile,
    SourceClaim,
    SourceDocument,
    SourceSegment,
    StrategyExperimentRun,
)
from .chunkers import get_chunker
from .evaluation import run_evaluation
from .ingestion import build_blocks
from .llm import llm
from .text import count_tokens, fingerprint

logger = logging.getLogger(__name__)


def _experiment_checksum(source_checksum: str, experiment_run_id: str) -> str:
    return hashlib.sha256(f"{source_checksum}:{experiment_run_id}".encode()).hexdigest()


def _claim_cache(db: Session) -> dict[str, list[dict]]:
    """段内容 fingerprint → 该段已有 claim 抽取结果与 embedding（主库 + 副本）。"""
    rows = db.execute(
        select(SourceClaim, SourceSegment).join(
            SourceSegment, SourceClaim.segment_id == SourceSegment.id
        )
    ).all()
    cache: dict[str, list[dict]] = {}
    for claim, segment in rows:
        cache.setdefault(fingerprint(segment.content), []).append(
            {
                "statement": claim.statement,
                "scope": claim.scope,
                "confidence": claim.confidence,
                "embedding": segment.embedding,
            }
        )
    return cache


def _add_claim(db: Session, segment: SourceSegment, statement: str, scope: str, confidence: float):
    db.add(
        SourceClaim(
            segment_id=segment.id,
            statement=statement,
            scope=scope[:200],
            confidence=confidence,
            fingerprint=fingerprint(statement),
        )
    )


def reprocess(
    db: Session, document_ids: list[str], policy_snapshot: dict, experiment_run_id: str
) -> dict:
    """按策略快照重建实验副本；同 experiment_run_id 重复执行先删旧副本再重建。"""
    stats = {"docs": 0, "segments": 0, "claims": 0, "cache_hits": 0, "llm_calls": 0}
    old_copies = db.scalars(
        select(SourceDocument).where(
            SourceDocument.metadata_json["experiment_run_id"].as_string() == experiment_run_id
        )
    ).all()
    for copy in old_copies:
        db.delete(copy)
    db.flush()

    cache = _claim_cache(db)
    for document_id in document_ids:
        source = db.get(SourceDocument, document_id)
        if source is None:
            raise ValueError(f"document not found: {document_id}")
        document_file = source.file or (
            db.get(DocumentFile, source.file_id) if source.file_id else None
        )
        if document_file is None:
            raise ValueError(f"文档缺少原始文件记录: {document_id}")
        raw = Path(document_file.storage_path).read_bytes()
        blocks, _, _ = build_blocks(
            document_file.filename,
            raw,
            document_file.mime,
            title=source.title,
            parser_strategy=policy_snapshot.get("parser_strategy", "auto"),
            min_text_chars=int(policy_snapshot.get("min_text_chars", 100)),
        )
        chunker = get_chunker(policy_snapshot.get("chunk_strategy", "heading"))
        drafts = chunker(blocks, dict(policy_snapshot.get("chunk_params") or {}))
        copy = SourceDocument(
            title=f"[实验] {source.title}"[:300],
            source_type=source.source_type,
            source_uri=source.source_uri,
            owner=source.owner,
            checksum=_experiment_checksum(source.checksum, experiment_run_id),
            status="processed",
            progress=100,
            metadata_json={
                "experiment_run_id": experiment_run_id,
                "experiment_source_id": source.id,
                "policy_name": policy_snapshot.get("name"),
            },
        )
        db.add(copy)
        db.flush()
        for ordinal, draft in enumerate(drafts):
            segment = SourceSegment(
                document_id=copy.id,
                ordinal=ordinal,
                heading=draft.heading,
                heading_path=draft.heading_path,
                segment_type=draft.segment_type,
                content=draft.content,
                token_count=count_tokens(draft.content),
            )
            db.add(segment)
            db.flush()
            cached = cache.get(fingerprint(draft.content))
            if cached:
                stats["cache_hits"] += 1
                segment.embedding = cached[0].get("embedding") or llm.embed(draft.content)
                for item in cached:
                    _add_claim(
                        db,
                        segment,
                        item["statement"],
                        item["scope"],
                        float(item["confidence"]),
                    )
                    stats["claims"] += 1
            else:
                stats["llm_calls"] += 1
                segment.embedding = llm.embed(draft.content)
                for item in llm.extract_claims(draft.content, draft.heading):
                    statement = str(item.get("statement", "")).strip()
                    if not statement:
                        continue
                    _add_claim(
                        db,
                        segment,
                        statement,
                        str(item.get("scope") or draft.heading or "general"),
                        float(item.get("confidence", 0.8)),
                    )
                    stats["claims"] += 1
            stats["segments"] += 1
        stats["docs"] += 1
    db.commit()
    return stats


def execute_experiment_run(db: Session, run_id: str) -> StrategyExperimentRun:
    """单个实验 Run：reprocess → 按 eval_group + 副本过滤评测 → metrics 落库。"""
    run = db.get(StrategyExperimentRun, run_id)
    if run is None:
        raise ValueError(f"experiment run not found: {run_id}")
    experiment = run.experiment
    try:
        run.status = "reprocessing"
        db.commit()
        stats = reprocess(
            db,
            list(experiment.document_ids or []),
            dict(run.policy_snapshot or {}),
            run.id,
        )
        run.status = "evaluating"
        db.commit()
        eval_run = run_evaluation(
            db, experiment_run_id=run.id, eval_group=experiment.eval_group
        )
        run.eval_run_id = eval_run.id
        run.metrics = {**eval_run.metrics, "reprocess": stats}
        run.status = "done"
        run.error_message = None
    except Exception as exc:
        db.rollback()
        run = db.get(StrategyExperimentRun, run_id)
        run.status = "failed"
        run.error_message = str(exc)[:500]
    db.commit()
    db.refresh(run)
    return run


def dispatch_experiment_run(db: Session, run: StrategyExperimentRun) -> None:
    if settings.ingest_execution_mode == "celery":
        from ..worker import execute_experiment_run_task

        execute_experiment_run_task.delay(run.id)
        return
    execute_experiment_run(db, run.id)
