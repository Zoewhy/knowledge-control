from __future__ import annotations

import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..database import SessionLocal
from ..models import SourceClaim, SourceSegment, WikiCitation, WikiSection
from ..schemas import CitationRead, SearchHit, SearchResponse
from .graph import expand_graph
from .llm import llm
from .search_index import search_index
from .text import lexical_score

DOMAIN_EXPANSIONS = [
    (("退入驻", "依赖"), "状态机 卡单 工作流 节点 超时"),
    (("CRM", "搜索"), "MySQL Elasticsearch 同步 索引 线索 版本"),
    (("保证金",), "商家入驻 智能助手 工具调用 实时信息"),
    (("授权", "链路"), "品牌 经销商 多级授权 中间授权 范围传递"),
    (("发券",), "营销 幂等键 回扫 对账 漏发 重复发放"),
]


@dataclass
class BranchResult:
    hits: list[SearchHit] = field(default_factory=list)
    elapsed_ms: float = 0
    error: str | None = None


def expand_domain_query(query: str) -> tuple[str, list[str]]:
    expansions: list[str] = []
    for keywords, addition in DOMAIN_EXPANSIONS:
        if all(keyword.lower() in query.lower() for keyword in keywords):
            expansions.append(addition)
    return " ".join([query, *expansions]), expansions


def dispatch_query(query: str) -> str:
    normalized = query.strip().lower()
    if normalized in {"hi", "hello", "你好", "您好", "谢谢", "thanks"}:
        return "direct_answer"
    return "knowledge_search"


def _citation(claim: SourceClaim) -> CitationRead:
    segment = claim.segment
    document = segment.document
    return CitationRead(
        claim_id=claim.id,
        statement=claim.statement,
        document_id=document.id,
        document_title=document.title,
        segment_id=segment.id,
        source_uri=document.source_uri,
    )


def _section_citations(db: Session, section_id: str) -> list[CitationRead]:
    citations = db.scalars(
        select(WikiCitation)
        .where(WikiCitation.section_id == section_id)
        .options(
            selectinload(WikiCitation.claim)
            .selectinload(SourceClaim.segment)
            .selectinload(SourceSegment.document)
        )
    ).all()
    return [_citation(citation.claim) for citation in citations]


def _segment_citations(db: Session, segment_id: str) -> list[CitationRead]:
    claims = db.scalars(
        select(SourceClaim)
        .where(SourceClaim.segment_id == segment_id)
        .options(selectinload(SourceClaim.segment).selectinload(SourceSegment.document))
    ).all()
    return [_citation(claim) for claim in claims]


def _wiki_lexical(query: str, top_k: int) -> list[SearchHit]:
    with SessionLocal() as db:
        sections = db.scalars(select(WikiSection).options(selectinload(WikiSection.page))).all()
        hits = []
        for section in sections:
            title_score = lexical_score(query, f"{section.page.title} {section.heading}")
            content_score = lexical_score(query, section.content)
            score = max(content_score, min(1.0, title_score * 1.25))
            if score > 0:
                hits.append(
                    SearchHit(
                        channel="wiki_lexical",
                        score=round(0.7 * score + 0.2, 6),
                        page_id=section.page_id,
                        section_id=section.id,
                        title=f"{section.page.title} / {section.heading}",
                        content=section.content,
                    )
                )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[: top_k * 2]


def _source_lexical(query: str, top_k: int) -> list[SearchHit]:
    with SessionLocal() as db:
        segments = db.scalars(
            select(SourceSegment).options(selectinload(SourceSegment.document))
        ).all()
        hits = []
        for segment in segments:
            title_score = lexical_score(query, f"{segment.document.title} {segment.heading or ''}")
            content_score = lexical_score(query, segment.content)
            score = max(content_score, min(1.0, title_score * 1.25))
            if score > 0:
                hits.append(
                    SearchHit(
                        channel="source_fallback",
                        score=round(0.55 * score + 0.1, 6),
                        segment_id=segment.id,
                        title=segment.document.title,
                        content=segment.content,
                    )
                )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[: top_k * 2]


def _wiki_vector(query: str, top_k: int) -> list[SearchHit]:
    if settings.is_sqlite:
        return []
    with SessionLocal() as db:
        distance = WikiSection.embedding.cosine_distance(llm.embed(query)).label("distance")
        rows = db.execute(
            select(WikiSection, distance)
            .where(WikiSection.embedding.is_not(None))
            .options(selectinload(WikiSection.page))
            .order_by(distance)
            .limit(top_k * 2)
        ).all()
        return [
            SearchHit(
                channel="wiki_vector",
                score=round(0.65 * max(0.0, 1.0 - float(raw_distance or 1.0)), 6),
                page_id=section.page_id,
                section_id=section.id,
                title=f"{section.page.title} / {section.heading}",
                content=section.content,
            )
            for section, raw_distance in rows
        ]


def _source_vector(query: str, top_k: int) -> list[SearchHit]:
    if settings.is_sqlite:
        return []
    with SessionLocal() as db:
        distance = SourceSegment.embedding.cosine_distance(llm.embed(query)).label("distance")
        rows = db.execute(
            select(SourceSegment, distance)
            .where(SourceSegment.embedding.is_not(None))
            .options(selectinload(SourceSegment.document))
            .order_by(distance)
            .limit(top_k * 2)
        ).all()
        return [
            SearchHit(
                channel="source_vector",
                score=round(0.55 * max(0.0, 1.0 - float(raw_distance or 1.0)), 6),
                segment_id=segment.id,
                title=segment.document.title,
                content=segment.content,
            )
            for segment, raw_distance in rows
        ]


def _elastic(query: str, top_k: int) -> list[SearchHit]:
    hits = []
    for item in search_index.search(query, top_k * 2):
        score = float(item["score"]) / (float(item["score"]) + 5.0)
        if item["kind"] == "section":
            hits.append(
                SearchHit(
                    channel="elasticsearch_bm25",
                    score=round(0.6 * score, 6),
                    page_id=item.get("page_id"),
                    section_id=item.get("section_id"),
                    title=f"{item['title']} / {item.get('heading', '')}",
                    content=item["content"],
                )
            )
        elif item["kind"] == "segment":
            hits.append(
                SearchHit(
                    channel="elasticsearch_bm25",
                    score=round(0.5 * score, 6),
                    segment_id=item.get("segment_id"),
                    title=item["title"],
                    content=item["content"],
                )
            )
    return hits


def _run_branch(name: str, query: str, top_k: int) -> BranchResult:
    started = time.perf_counter()
    handlers = {
        "wiki_lexical": _wiki_lexical,
        "source_lexical": _source_lexical,
        "wiki_vector": _wiki_vector,
        "source_vector": _source_vector,
        "elasticsearch_bm25": _elastic,
    }
    try:
        hits = handlers[name](query, top_k)
        return BranchResult(
            hits=hits, elapsed_ms=round((time.perf_counter() - started) * 1000, 2)
        )
    except Exception as exc:
        return BranchResult(
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error=f"{type(exc).__name__}: {exc}",
        )


def _merge(merged: dict[str, SearchHit], hit: SearchHit) -> None:
    key = f"wiki:{hit.section_id}" if hit.section_id else f"source:{hit.segment_id}"
    if key not in merged:
        merged[key] = hit.model_copy(deep=True)
        return
    current = merged[key]
    current.score = round(min(1.0, max(current.score, hit.score) + 0.2 * hit.score), 6)
    current.channel = "+".join(dict.fromkeys([*current.channel.split("+"), hit.channel]))


def hybrid_search(
    db: Session, query: str, top_k: int = 8, include_answer: bool = True
) -> SearchResponse:
    start = time.perf_counter()
    trace_id = uuid.uuid4().hex
    route = dispatch_query(query)
    if route == "direct_answer":
        return SearchResponse(
            query=query,
            answer="该请求无需检索知识库。" if include_answer else None,
            hits=[],
            diagnostics={"trace_id": trace_id, "dispatcher": route, "branches": {}},
            latency_ms=round((time.perf_counter() - start) * 1000, 2),
        )

    retrieval_query, expansions = expand_domain_query(query)
    branch_names = [
        "wiki_lexical",
        "source_lexical",
        "wiki_vector",
        "source_vector",
        "elasticsearch_bm25",
    ]
    executor = ThreadPoolExecutor(max_workers=settings.retrieval_max_workers)
    futures = {
        name: executor.submit(_run_branch, name, retrieval_query, top_k) for name in branch_names
    }
    results: dict[str, BranchResult] = {}
    for name, future in futures.items():
        try:
            results[name] = future.result(timeout=settings.retrieval_timeout_seconds)
        except Exception as exc:
            results[name] = BranchResult(error=f"{type(exc).__name__}: independent timeout")
    executor.shutdown(wait=False, cancel_futures=True)

    merged: dict[str, SearchHit] = {}
    source_candidates: dict[str, SearchHit] = {}
    for name, result in results.items():
        target = source_candidates if name.startswith("source_") else merged
        for hit in result.hits:
            if hit.segment_id:
                _merge(source_candidates, hit)
            else:
                _merge(target, hit)

    seed_section_ids = {hit.section_id for hit in merged.values() if hit.section_id}
    graph_section_ids, graph_paths = expand_graph(db, seed_section_ids)
    if graph_section_ids:
        graph_sections = db.scalars(
            select(WikiSection)
            .where(WikiSection.id.in_(graph_section_ids))
            .options(selectinload(WikiSection.page))
        ).all()
        for section in graph_sections:
            _merge(
                merged,
                SearchHit(
                    channel="graph_expansion",
                    score=0.36,
                    page_id=section.page_id,
                    section_id=section.id,
                    title=f"{section.page.title} / {section.heading}",
                    content=section.content,
                ),
            )

    best_wiki_score = max((hit.score for hit in merged.values()), default=0.0)
    fallback_used = best_wiki_score < settings.wiki_fallback_score
    if fallback_used:
        for hit in source_candidates.values():
            _merge(merged, hit)

    for hit in merged.values():
        citations = (
            _section_citations(db, hit.section_id)
            if hit.section_id
            else _segment_citations(db, hit.segment_id)
        )
        hit.citations = citations
        hit.source = citations[0] if citations else None

    ranked = sorted(merged.values(), key=lambda item: item.score, reverse=True)
    hits: list[SearchHit] = []
    group_counts: dict[str, int] = defaultdict(int)
    for hit in ranked:
        group = f"page:{hit.page_id}" if hit.page_id else f"source:{hit.segment_id}"
        limit = 2 if hit.page_id else 1
        if group_counts[group] >= limit:
            continue
        hits.append(hit)
        group_counts[group] += 1
        if len(hits) >= top_k:
            break
    contexts = [{"title": hit.title, "content": hit.content} for hit in hits]
    answer = llm.answer(query, contexts) if include_answer else None
    diagnostics = {
        "trace_id": trace_id,
        "dispatcher": route if hits else "no_hit",
        "channels": {
            name: len(result.hits) for name, result in results.items() if result.hits
        },
        "branches": {
            name: {
                "elapsed_ms": result.elapsed_ms,
                "hits": len(result.hits),
                "error": result.error,
                "degraded": bool(result.error),
            }
            for name, result in results.items()
        },
        "fallback_used": fallback_used,
        "llm_mode": "bailian" if llm.enabled else "mock",
        "query_expansions": expansions,
        "graph_paths": graph_paths,
    }
    return SearchResponse(
        query=query,
        answer=answer,
        hits=hits,
        diagnostics=diagnostics,
        latency_ms=round((time.perf_counter() - start) * 1000, 2),
    )
