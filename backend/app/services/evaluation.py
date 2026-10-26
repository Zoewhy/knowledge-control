import math
import re
from collections import defaultdict
from statistics import mean
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EvaluationCase, EvaluationResult, EvaluationRun, utcnow
from ..schemas import SearchResponse
from .retrieval import hybrid_search

ARM_FLAGS: dict[str, tuple[bool, bool]] = {
    "G0": (True, True),
    "G1": (False, True),
    "G2": (True, False),
    "G3": (False, False),
}

SCORER_NAMES = ("source_hit", "wiki_hit", "keyword_hit", "citation_validity", "rag_no_hit")


def _aggregate_by_type(rows: list[dict]) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["question_type"]].append(row)
    return {
        question_type: {
            "count": len(items),
            "pass_rate": round(mean([item["pass"] for item in items]), 4),
            **{
                name: round(mean([item[name] for item in items]), 4)
                for name in SCORER_NAMES
            },
        }
        for question_type, items in sorted(grouped.items())
    }


def source_hit(case: EvaluationCase, response: SearchResponse) -> float:
    source_titles = {
        citation.document_title
        for hit in response.hits
        for citation in hit.citations
    }
    source_titles.update(hit.title for hit in response.hits)
    return float(
        not case.expected_sources
        or any(expected in source_titles for expected in case.expected_sources)
    )


def wiki_hit(_: EvaluationCase, response: SearchResponse) -> float:
    return float(any(hit.section_id for hit in response.hits))


def keyword_hit(case: EvaluationCase, response: SearchResponse) -> float:
    answer = (response.answer or "").lower()
    return float(
        not case.expected_keywords
        or all(keyword.lower() in answer for keyword in case.expected_keywords)
    )


def citation_validity(_: EvaluationCase, response: SearchResponse) -> float:
    if not response.hits or not response.answer:
        return 0.0
    references = {int(value) for value in re.findall(r"\[(\d+)\]", response.answer)}
    if not references or any(index < 1 or index > len(response.hits) for index in references):
        return 0.0
    for index in references:
        hit = response.hits[index - 1]
        valid = any(
            citation.claim_id and citation.segment_id and citation.document_id
            for citation in hit.citations
        )
        if not valid:
            return 0.0
    return 1.0


def rag_no_hit(_: EvaluationCase, response: SearchResponse) -> float:
    return float(not response.hits)


Scorer = Callable[[EvaluationCase, SearchResponse], float]


class ScorerRegistry:
    def __init__(self) -> None:
        self._scorers: dict[str, Scorer] = {}

    def register(self, name: str, scorer: Scorer) -> None:
        self._scorers[name] = scorer

    def score(self, case: EvaluationCase, response: SearchResponse) -> dict[str, float]:
        return {name: scorer(case, response) for name, scorer in self._scorers.items()}


scorer_registry = ScorerRegistry()
scorer_registry.register("source_hit", source_hit)
scorer_registry.register("wiki_hit", wiki_hit)
scorer_registry.register("keyword_hit", keyword_hit)
scorer_registry.register("citation_validity", citation_validity)
scorer_registry.register("rag_no_hit", rag_no_hit)


class EvaluationEngine:
    def __init__(self, registry: ScorerRegistry) -> None:
        self.registry = registry

    def run(self, db: Session, arm: str = "G3") -> EvaluationRun:
        if arm not in ARM_FLAGS:
            raise ValueError(f"unknown evaluation arm: {arm}")
        disable_wiki, disable_graph = ARM_FLAGS[arm]
        run = EvaluationRun()
        db.add(run)
        db.flush()
        cases = list(
            db.scalars(select(EvaluationCase).where(EvaluationCase.enabled.is_(True))).all()
        )
        rows: list[dict] = []
        try:
            for case in cases:
                response = hybrid_search(
                    db,
                    case.query,
                    top_k=8,
                    disable_wiki=disable_wiki,
                    disable_graph=disable_graph,
                )
                metrics = self.registry.score(case, response)
                metrics["latency_ms"] = response.latency_ms
                metrics["question_type"] = case.question_type or "general"
                metrics["pass"] = float(
                    metrics["source_hit"]
                    and metrics["keyword_hit"]
                    and metrics["citation_validity"]
                )
                rows.append(metrics)
                db.add(
                    EvaluationResult(
                        run_id=run.id,
                        case_id=case.id,
                        metrics=metrics,
                        answer=response.answer or "",
                        trace={
                            **response.diagnostics,
                            "hits": [
                                {
                                    "title": hit.title,
                                    "channel": hit.channel,
                                    "citation_ids": [
                                        citation.claim_id for citation in hit.citations
                                    ],
                                }
                                for hit in response.hits
                            ],
                        },
                    )
                )
            latencies = sorted(row["latency_ms"] for row in rows)
            p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
            run.metrics = {
                "arm": arm,
                "cases": len(rows),
                **{
                    name: round(mean([row[name] for row in rows]), 4) if rows else 0
                    for name in (
                        "source_hit",
                        "wiki_hit",
                        "citation_validity",
                        "pass",
                        "rag_no_hit",
                    )
                },
                "pass_rate": round(mean([row["pass"] for row in rows]), 4) if rows else 0,
                "avg_latency_ms": round(mean(latencies), 2) if rows else 0,
                "p95_latency_ms": round(latencies[p95_index], 2) if rows else 0,
                "by_type": _aggregate_by_type(rows),
            }
            run.status = "succeeded"
        except Exception as exc:
            run.status = "failed"
            run.metrics = {"error": str(exc), "cases": len(rows)}
            run.finished_at = utcnow()
            db.commit()
            raise
        run.finished_at = utcnow()
        db.commit()
        db.refresh(run)
        return run


engine = EvaluationEngine(scorer_registry)


def run_evaluation(db: Session, arm: str = "G3") -> EvaluationRun:
    return engine.run(db, arm=arm)
