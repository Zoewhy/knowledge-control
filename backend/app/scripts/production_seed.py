from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select

from app.database import Base, SessionLocal, engine
from app.models import (
    AgentTask,
    EvaluationCase,
    ReviewStatus,
    ReviewTask,
    SourceClaim,
    SourceDocument,
    SourceSegment,
    WikiCitation,
    WikiPage,
)
from app.services.agent_runtime import create_task, execute_task
from app.services.compiler import compile_claims, decide_review
from app.services.ingestion import ingest_document

from .merchant_seed_data import AGENT_INSTRUCTIONS, EVALUATION_CASES, PROFILES, build_documents

COMPILED_SOURCE_TYPES = {"public_regulation", "synthetic_sop"}
LEGACY_SOURCE_URIS = {"seed://incident-guide", "seed://autocase-guide"}
LEGACY_EVALUATION_NAMES = {"告警归因", "用例优先级"}


def _count(db, model, *conditions) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*conditions)) or 0


def _remove_legacy_algorithm_demo(db) -> dict[str, int]:
    documents = list(
        db.scalars(select(SourceDocument).where(SourceDocument.source_uri.in_(LEGACY_SOURCE_URIS)))
    )
    claim_ids = {
        claim.id
        for document in documents
        for segment in document.segments
        for claim in segment.claims
    }
    citations = (
        list(db.scalars(select(WikiCitation).where(WikiCitation.claim_id.in_(claim_ids))).all())
        if claim_ids
        else []
    )
    page_ids = {citation.section.page_id for citation in citations}
    reviews = (
        list(db.scalars(select(ReviewTask).where(ReviewTask.claim_id.in_(claim_ids))).all())
        if claim_ids
        else []
    )
    evaluations = list(
        db.scalars(
            select(EvaluationCase).where(EvaluationCase.name.in_(LEGACY_EVALUATION_NAMES))
        ).all()
    )
    for item in reviews + citations + evaluations:
        db.delete(item)
    db.flush()
    removed_pages = 0
    for page_id in page_ids:
        page = db.get(WikiPage, page_id)
        if page and not any(section.citations for section in page.sections):
            db.delete(page)
            removed_pages += 1
    for document in documents:
        db.delete(document)
    db.commit()
    return {
        "documents": len(documents),
        "claims": len(claim_ids),
        "reviews": len(reviews),
        "wiki_pages": removed_pages,
        "evaluation_cases": len(evaluations),
    }


def seed(scale: str) -> dict:
    Base.metadata.create_all(bind=engine)
    documents = build_documents(scale)
    created_documents = 0
    claim_ids: list[str] = []

    with SessionLocal() as db:
        legacy_removed = _remove_legacy_algorithm_demo(db)
        for payload in documents:
            document, created = ingest_document(db, payload)
            if not created:
                continue
            created_documents += 1
            if payload.source_type in COMPILED_SOURCE_TYPES:
                claim_ids.extend(
                    claim.id for segment in document.segments for claim in segment.claims
                )

        noisy_reviews = list(
            db.scalars(
                select(ReviewTask)
                .join(SourceClaim, ReviewTask.claim_id == SourceClaim.id)
                .join(SourceSegment, SourceClaim.segment_id == SourceSegment.id)
                .join(SourceDocument, SourceSegment.document_id == SourceDocument.id)
                .where(
                    ReviewTask.status == ReviewStatus.pending,
                    SourceDocument.source_type.not_in(COMPILED_SOURCE_TYPES),
                    SourceDocument.metadata_json["synthetic"].as_boolean().is_(True),
                )
            ).all()
        )
        for review in noisy_reviews:
            db.delete(review)

        scope_reviews = list(
            db.execute(
                select(ReviewTask, SourceClaim.id)
                .join(SourceClaim, ReviewTask.claim_id == SourceClaim.id)
                .join(SourceSegment, SourceClaim.segment_id == SourceSegment.id)
                .join(SourceDocument, SourceSegment.document_id == SourceDocument.id)
                .where(
                    ReviewTask.status == ReviewStatus.pending,
                    ReviewTask.risk_score > 0.72,
                    SourceDocument.source_type == "synthetic_sop",
                )
            ).all()
        )
        for review, claim_id in scope_reviews:
            db.delete(review)
            claim_ids.append(claim_id)
        db.commit()

        claim_ids = list(dict.fromkeys(claim_ids))

        compile_summary = {
            "auto_merged": 0,
            "review_created": 0,
            "duplicates": 0,
            "source_links": 0,
        }
        for offset in range(0, len(claim_ids), 80):
            result = compile_claims(db, claim_ids[offset : offset + 80])
            for field in compile_summary:
                compile_summary[field] += getattr(result, field)

        low_risk_reviews = list(
            db.scalars(
                select(ReviewTask)
                .join(SourceClaim, ReviewTask.claim_id == SourceClaim.id)
                .join(SourceSegment, SourceClaim.segment_id == SourceSegment.id)
                .join(SourceDocument, SourceSegment.document_id == SourceDocument.id)
                .where(
                    ReviewTask.status == ReviewStatus.pending,
                    ReviewTask.risk_score <= 0.5,
                    SourceDocument.source_type == "synthetic_sop",
                )
            ).all()
        )
        for review in low_risk_reviews:
            decide_review(
                db,
                review,
                decision="source_only",
                reviewer="synthetic-dataset-governor",
                note="低风险同主题增量，仅补充来源；由生产数据治理策略自动处理。",
            )

        created_evaluations = 0
        for name, query, expected_sources in EVALUATION_CASES:
            if db.scalar(select(EvaluationCase).where(EvaluationCase.name == name)):
                continue
            db.add(
                EvaluationCase(
                    name=name,
                    query=query,
                    expected_sources=expected_sources,
                    expected_keywords=[],
                )
            )
            created_evaluations += 1
        db.commit()

        created_tasks = 0
        for instruction in AGENT_INSTRUCTIONS:
            if db.scalar(select(AgentTask).where(AgentTask.instruction == instruction)):
                continue
            task = create_task(db, instruction, {"dataset": "merchant-production-seed-v1"})
            execute_task(db, task)
            created_tasks += 1

        return {
            "scale": scale,
            "profile": PROFILES[scale],
            "created": {
                "documents": created_documents,
                "claims": len(claim_ids),
                "source_only_documents": len(documents)
                - sum(item.source_type in COMPILED_SOURCE_TYPES for item in documents),
                "reconciled_noisy_reviews": len(noisy_reviews),
                "recompiled_scope_reviews": len(scope_reviews),
                "auto_resolved_low_risk_reviews": len(low_risk_reviews),
                "evaluation_cases": created_evaluations,
                "agent_tasks": created_tasks,
                **compile_summary,
            },
            "totals": {
                "documents": _count(db, SourceDocument),
                "claims": _count(db, SourceClaim),
                "wiki_pages": _count(db, WikiPage),
                "pending_reviews": _count(
                    db, ReviewTask, ReviewTask.status == ReviewStatus.pending
                ),
                "evaluation_cases": _count(db, EvaluationCase),
                "agent_tasks": _count(db, AgentTask),
            },
            "data_policy": {
                "public_documents": "公开法规摘要，保留权威来源链接",
                "business_records": "确定性生成的合成数据，已脱敏且不代表真实线上记录",
            },
            "legacy_algorithm_demo_removed": legacy_removed,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed merchant-domain production-like data")
    parser.add_argument("--scale", choices=sorted(PROFILES), default="standard")
    args = parser.parse_args()
    print(json.dumps(seed(args.scale), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
