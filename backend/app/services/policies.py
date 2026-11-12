"""入库策略解析：document 指定 > doc_pattern 命中 > 内置 default。"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import IngestPolicy, SourceDocument

DEFAULT_POLICY_NAME = "default"
DEFAULT_POLICY_VALUES = {
    "name": DEFAULT_POLICY_NAME,
    "doc_pattern": "*",
    "parser_strategy": "auto",
    "chunk_strategy": "heading",
    "chunk_params": {"max_tokens": 512, "overlap_tokens": 50},
    "table_mode": "chunk",
    "min_text_chars": 100,
    "auto_compile": True,
}


def ensure_default_policy(db: Session) -> IngestPolicy:
    policy = db.scalar(select(IngestPolicy).where(IngestPolicy.name == DEFAULT_POLICY_NAME))
    if policy is None:
        policy = IngestPolicy(**DEFAULT_POLICY_VALUES)
        db.add(policy)
        db.flush()
    return policy


def _document_filename(document: SourceDocument) -> str:
    file = getattr(document, "file", None)
    if file is not None and file.filename:
        return file.filename
    return str((document.metadata_json or {}).get("filename") or "")


def _pattern_score(policy: IngestPolicy, document: SourceDocument) -> int | None:
    """命中返回具体度（越大越具体），未命中返回 None。"""
    pattern = (policy.doc_pattern or "*").strip()
    if pattern == "*":
        return 0
    source_type = document.source_type or ""
    if pattern == f"source_type:{source_type}" or pattern == source_type:
        return 3
    filename = _document_filename(document)
    extension = Path(filename).suffix.lstrip(".").lower()
    if extension and pattern.lower() == extension:
        return 2
    if filename and fnmatch(filename.lower(), pattern.lower()):
        return 1
    return None


def resolve_policy(db: Session, document: SourceDocument) -> IngestPolicy:
    """document.metadata_json["policy_id"] > doc_pattern 具体度最高者 > default。"""
    policy_id = (document.metadata_json or {}).get("policy_id")
    if policy_id:
        policy = db.get(IngestPolicy, str(policy_id))
        if policy is not None:
            return policy
    best: tuple[int, IngestPolicy] | None = None
    policies = db.scalars(
        select(IngestPolicy).where(IngestPolicy.name != DEFAULT_POLICY_NAME)
    ).all()
    for policy in policies:
        score = _pattern_score(policy, document)
        if score is None:
            continue
        if best is None or score > best[0]:
            best = (score, policy)
    if best is not None:
        return best[1]
    return ensure_default_policy(db)


def should_auto_compile(db: Session, document: SourceDocument) -> bool:
    return bool(resolve_policy(db, document).auto_compile)


def policy_snapshot(policy: IngestPolicy) -> dict:
    return {
        "id": policy.id,
        "name": policy.name,
        "doc_pattern": policy.doc_pattern,
        "parser_strategy": policy.parser_strategy,
        "chunk_strategy": policy.chunk_strategy,
        "chunk_params": dict(policy.chunk_params or {}),
        "table_mode": policy.table_mode,
        "min_text_chars": policy.min_text_chars,
        "auto_compile": policy.auto_compile,
        "version": policy.version,
    }
