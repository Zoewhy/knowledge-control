from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    PatchAction,
    ReviewStatus,
    ReviewTask,
    SourceClaim,
    WikiCitation,
    WikiPage,
    WikiSection,
    utcnow,
)
from .graph import index_claim_graph
from .llm import llm
from .search_index import search_index
from .text import lexical_score, slugify

NEW_PAGE_SCORE_THRESHOLD = 0.28
AUTO_MERGE_SCORE_THRESHOLD = 0.45
HIGH_RISK_TERMS = ("删除", "禁用", "资金", "赔付", "自动通过", "回滚", "权限", "高风险")


@dataclass
class CompileResult:
    auto_merged: int = 0
    review_created: int = 0
    duplicates: int = 0
    source_links: int = 0


def _dedupe_key(claim: SourceClaim, page_id: str | None, action: str, content: str) -> str:
    content_hash = hashlib.sha256(content.strip().encode()).hexdigest()
    value = f"{claim.fingerprint}:{page_id}:{action}:{content_hash}"
    return hashlib.sha256(value.encode()).hexdigest()


def _link_claim(db: Session, section: WikiSection, claim: SourceClaim) -> bool:
    found = db.scalar(
        select(WikiCitation).where(
            WikiCitation.section_id == section.id, WikiCitation.claim_id == claim.id
        )
    )
    if not found:
        db.add(WikiCitation(section_id=section.id, claim_id=claim.id))
    index_claim_graph(db, section, claim)
    return not found


def _create_review(
    db: Session,
    claim: SourceClaim,
    page: WikiPage | None,
    section: WikiSection | None,
    action: PatchAction,
    reason: str,
    risk: float,
) -> bool:
    key = _dedupe_key(claim, page.id if page else None, action.value, claim.statement)
    exists = db.scalar(select(ReviewTask).where(ReviewTask.dedupe_key == key))
    if exists:
        return False
    db.add(
        ReviewTask(
            dedupe_key=key,
            claim_id=claim.id,
            target_page_id=page.id if page else None,
            target_section_id=section.id if section else None,
            action=action,
            reason=reason,
            proposed_content=claim.statement,
            current_content=section.content if section else "",
            risk_score=risk,
        )
    )
    db.flush()
    return True


def _index_section(section: WikiSection, page: WikiPage) -> None:
    section.embedding = llm.embed(section.content)
    search_index.index_section(
        section.id, page.id, page.title, section.heading, section.content
    )


def _new_page(db: Session, claim: SourceClaim) -> tuple[WikiPage, WikiSection]:
    slug_base = slugify(claim.scope)
    slug = slug_base
    suffix = 1
    while db.scalar(select(WikiPage).where(WikiPage.slug == slug)):
        suffix += 1
        slug = f"{slug_base}-{suffix}"
    page = WikiPage(title=claim.scope, slug=slug, summary=claim.statement[:180])
    section = WikiSection(
        page=page,
        heading="知识说明",
        content=claim.statement,
        ordinal=0,
        embedding=llm.embed(claim.statement),
    )
    db.add(page)
    db.flush()
    _link_claim(db, section, claim)
    search_index.index_section(
        section.id, page.id, page.title, section.heading, section.content
    )
    return page, section


def compile_claims(db: Session, claim_ids: list[str]) -> CompileResult:
    result = CompileResult()
    pages = list(db.scalars(select(WikiPage)).all())
    claims = list(db.scalars(select(SourceClaim).where(SourceClaim.id.in_(claim_ids))).all())

    for claim in claims:
        duplicate_claim = db.scalar(
            select(SourceClaim).where(
                SourceClaim.fingerprint == claim.fingerprint, SourceClaim.id != claim.id
            )
        )
        candidates: list[tuple[float, WikiPage, WikiSection]] = []
        for page in pages:
            for section in page.sections:
                score = max(
                    lexical_score(claim.scope, f"{page.title} {section.heading}"),
                    lexical_score(claim.statement, section.content),
                )
                candidates.append((score, page, section))
        candidates.sort(key=lambda item: item[0], reverse=True)
        best = candidates[0] if candidates else None

        if duplicate_claim and best and best[0] >= 0.55:
            if _link_claim(db, best[2], claim):
                result.source_links += 1
            result.duplicates += 1
            continue

        scope_unclear = claim.scope.strip().lower() in {"", "general", "unknown", "未明确"}
        if not best or best[0] < NEW_PAGE_SCORE_THRESHOLD:
            if claim.confidence >= 0.72 and not scope_unclear:
                page, _ = _new_page(db, claim)
                pages.append(page)
                result.auto_merged += 1
            elif _create_review(
                db, claim, None, None, PatchAction.create_page, "Scope 不明确或抽取置信度较低", 0.7
            ):
                result.review_created += 1
            continue

        score, page, section = best
        content_similarity = lexical_score(claim.statement, section.content)
        high_risk = any(term in claim.statement for term in HIGH_RISK_TERMS)
        if content_similarity >= 0.7:
            if _link_claim(db, section, claim):
                result.source_links += 1
            result.duplicates += 1
        elif high_risk or scope_unclear:
            reason = "包含高风险规则或权限变更" if high_risk else "Scope 不明确"
            if _create_review(
                db, claim, page, section, PatchAction.update_section, reason, max(0.75, 1 - score)
            ):
                result.review_created += 1
        elif score >= AUTO_MERGE_SCORE_THRESHOLD and claim.confidence >= 0.8:
            scope_score = lexical_score(claim.scope, f"{page.title} {section.heading}")
            if claim.scope not in {page.title, section.heading} and scope_score < 0.2:
                ordinal = db.scalar(
                    select(func.max(WikiSection.ordinal)).where(WikiSection.page_id == page.id)
                )
                section = WikiSection(
                    page_id=page.id,
                    heading=claim.scope,
                    content=claim.statement,
                    ordinal=(ordinal if ordinal is not None else -1) + 1,
                )
                db.add(section)
                db.flush()
            else:
                section.content = f"{section.content}\n\n{claim.statement}"
            page.version += 1
            _link_claim(db, section, claim)
            _index_section(section, page)
            result.auto_merged += 1
        else:
            action = (
                PatchAction.append_section
                if claim.scope not in {page.title, section.heading}
                else PatchAction.update_section
            )
            if _create_review(
                db,
                claim,
                page,
                section,
                action,
                "与已有知识相关但存在潜在冲突或需要新增 Section",
                round(1 - min(score, 1), 2),
            ):
                result.review_created += 1
    db.commit()
    return result


def decide_review(
    db: Session,
    task: ReviewTask,
    decision: str,
    reviewer: str,
    note: str,
    scope: str | None = None,
    owner: str | None = None,
    target_section_id: str | None = None,
    proposed_content: str | None = None,
) -> None:
    if task.status != ReviewStatus.pending:
        raise ValueError("该审核任务已处理")
    claim = db.get(SourceClaim, task.claim_id)
    if not claim:
        raise ValueError("来源 Claim 不存在")
    section_id = target_section_id or task.target_section_id
    section = db.get(WikiSection, section_id) if section_id else None
    page = db.get(WikiPage, section.page_id) if section else db.get(WikiPage, task.target_page_id)
    if target_section_id and not section:
        raise ValueError("目标 Section 不存在")
    content = (proposed_content or task.proposed_content).strip()
    if not content:
        raise ValueError("拟合并内容不能为空")
    if scope:
        claim.scope = scope.strip()[:200]

    if decision in {"approve", "source_only"}:
        if not section and decision == "approve":
            page, section = _new_page(db, claim)
            section.content = content
            _index_section(section, page)
        elif section and decision == "approve":
            page = section.page
            if task.action == PatchAction.append_section:
                ordinal = db.scalar(
                    select(func.max(WikiSection.ordinal)).where(WikiSection.page_id == page.id)
                )
                section = WikiSection(
                    page_id=page.id,
                    heading=claim.scope,
                    content=content,
                    ordinal=(ordinal if ordinal is not None else -1) + 1,
                )
                db.add(section)
                db.flush()
            else:
                section.content = f"{section.content}\n\n{content}"
            page.version += 1
            _index_section(section, page)
        if owner and page:
            page.owner = owner.strip()[:120]
        if section:
            _link_claim(db, section, claim)
            search_index.index_section(
                section.id, section.page.id, section.page.title, section.heading, section.content
            )
        task.status = ReviewStatus.approved
    else:
        task.status = ReviewStatus.rejected
    task.reviewer = reviewer
    task.decision_note = note
    task.decided_at = utcnow()
    db.commit()
