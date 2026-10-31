from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AgentTask, AlertEvent, IncidentRecord, WikiCitation
from .feishu import FeishuError, feishu_client
from .graph import graph_store, query_graph
from .llm import llm
from .retrieval import hybrid_search
from .text import lexical_score

logger = logging.getLogger(__name__)


class HypothesisDraft(BaseModel):
    summary: str = ""
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    impact: str = ""
    recommended_actions: list[str] = Field(default_factory=list)
    uncertainty: str = ""


_HYPOTHESIS_RULES: list[tuple[str, list[str]]] = [
    ("召回", ["索引更新延迟或构建失败", "特征缺失或上游数据异常", "模型版本发布引入回归"]),
    ("超时", ["下游依赖响应变慢", "连接池或线程池耗尽", "流量突增导致排队"]),
    ("错误率", ["最近发布引入缺陷", "依赖服务故障", "配置变更错误"]),
    ("库存", ["库存扣减并发冲突", "缓存与数据库不一致"]),
    ("支付", ["支付渠道抖动", "回调处理失败堆积"]),
]
_DEFAULT_HYPOTHESES = ["配置或发布变更", "资源水位不足", "依赖服务异常"]


def _get_alert(db: Session, alert_id: str) -> AlertEvent:
    alert = db.get(AlertEvent, alert_id)
    if not alert:
        raise ValueError(f"告警不存在: {alert_id}")
    return alert


def alert_context_collect(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    alert = _get_alert(db, str(args["alert_id"]))
    return {
        "alert_id": alert.id,
        "title": alert.title,
        "service": alert.service,
        "severity": alert.severity,
        "status": alert.status,
        "description": alert.description[:500],
        "labels": alert.labels,
        "occurrence_count": alert.occurrence_count,
    }


def _fallback_hypotheses(alert: AlertEvent) -> list[dict[str, Any]]:
    text = f"{alert.title} {alert.description}"
    causes: list[str] = []
    for keyword, items in _HYPOTHESIS_RULES:
        if keyword in text:
            causes.extend(items)
    if not causes:
        causes = list(_DEFAULT_HYPOTHESES)
    return [
        {"cause": cause, "evidence": "基于告警标题与经验的候选假设，待证据确认"}
        for cause in causes[:3]
    ]


def _similar_incidents_lexical(
    db: Session, alert: AlertEvent, limit: int = 5, threshold: float = 0.5
) -> list[dict[str, Any]]:
    """新事件尚未建图边，用 alert 文本在历史 IncidentRecord 里做词面匹配。"""
    text = f"{alert.title} {alert.service}"
    matched: list[dict[str, Any]] = []
    for incident in db.scalars(select(IncidentRecord)).all():
        score = lexical_score(text, f"{incident.title} {incident.summary}")
        if score >= threshold:
            matched.append(
                {
                    "incident_id": incident.id,
                    "title": incident.title,
                    "score": round(score, 4),
                    "root_cause_hypothesis": incident.root_cause_hypothesis,
                }
            )
    return sorted(matched, key=lambda item: item["score"], reverse=True)[:limit]


def incident_hypothesis_generate(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    alert = _get_alert(db, str(args["alert_id"]))
    query_text = f"{alert.title} {alert.service}".strip()
    knowledge = hybrid_search(db, query_text, top_k=5)
    references = [
        {
            "title": hit.title,
            "channel": hit.channel,
            "score": round(hit.score, 4),
            "citations": [citation.model_dump() for citation in hit.citations[:2]],
        }
        for hit in knowledge.hits
    ]
    dependency_chain = graph_store().dependency_chain(db, alert.service)
    similar_incidents = _similar_incidents_lexical(db, alert)
    fallback = HypothesisDraft(
        summary=(
            f"{alert.severity} 告警「{alert.title}」"
            f"（服务 {alert.service}，出现 {alert.occurrence_count} 次）"
        ),
        hypotheses=_fallback_hypotheses(alert),
        impact=f"影响服务 {alert.service}，具体范围需结合监控与调用链确认。",
        recommended_actions=[
            "核对最近 24 小时发布与配置变更",
            "查看服务监控与下游依赖水位",
            "按关联知识文档执行标准排查步骤",
        ],
        uncertainty="当前输出为根因假设，未接入实时监控数据，不能视为已确认根因。",
    ).model_dump()
    generated = llm.structured(
        "你是稳定性排查专家。基于告警信息和知识上下文给出根因假设、影响面和排查动作。"
        "只返回 JSON："
        "{summary,hypotheses:[{cause,evidence}],impact,recommended_actions:[...],uncertainty}。"
        "只能输出根因假设，禁止声称根因已确认。",
        f"告警：{fallback['summary']}\n描述：{alert.description}\n"
        f"知识：{[hit.content for hit in knowledge.hits]}\n"
        f"依赖链：{dependency_chain}\n"
        f"相似事件：{[item['title'] for item in similar_incidents]}",
        fallback,
    )
    try:
        triage = HypothesisDraft.model_validate(generated).model_dump()
        if not triage["hypotheses"]:
            raise ValidationError.from_exception_data("HypothesisDraft", [])
    except ValidationError:
        triage = fallback
    triage["knowledge_references"] = references
    triage["graph_paths"] = query_graph(db, query_text, limit=5)
    triage["dependency_chain"] = dependency_chain
    triage["similar_incidents"] = similar_incidents
    alert.triage = triage
    alert.status = "triaged"
    db.commit()
    return {"alert_id": alert.id, "triage": triage}


def incident_record_create(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    alert = _get_alert(db, str(args["alert_id"]))
    triage = alert.triage or {}
    if not triage.get("hypotheses"):
        raise ValueError("缺少归因假设，请先执行 incident_hypothesis_generate")
    existing = (
        db.query(IncidentRecord)
        .filter(IncidentRecord.alert_id == alert.id, IncidentRecord.task_id == args.get("task_id"))
        .first()
    )
    if existing:
        return {"incident_id": existing.id, "reused": True}
    incident = IncidentRecord(
        alert_id=alert.id,
        task_id=args.get("task_id"),
        title=alert.title,
        severity=alert.severity,
        summary=str(triage.get("summary") or ""),
        root_cause_hypothesis="；".join(
            str(item.get("cause", "")) for item in triage.get("hypotheses", []) if item.get("cause")
        ),
        impact=str(triage.get("impact") or ""),
        recommended_actions=[str(item) for item in triage.get("recommended_actions", [])],
        references={
            "knowledge_references": triage.get("knowledge_references", []),
            "graph_paths": triage.get("graph_paths", []),
            "uncertainty": triage.get("uncertainty", ""),
        },
    )
    db.add(incident)
    alert.status = "recorded"
    db.commit()
    db.refresh(incident)
    try:
        similar: list[tuple[str, float]] = []
        for other in db.scalars(
            select(IncidentRecord).where(IncidentRecord.id != incident.id)
        ).all():
            score = lexical_score(
                f"{incident.title} {incident.summary}",
                f"{other.title} {other.summary}",
            )
            if score >= 0.5:
                similar.append((other.id, round(score, 4)))
        graph_store().register_incident(db, incident.id, incident.title, similar)
        claim_ids = [
            citation.get("claim_id")
            for reference in triage.get("knowledge_references", [])
            for citation in reference.get("citations", [])
            if citation.get("claim_id")
        ]
        section_ids = (
            list(
                db.scalars(
                    select(WikiCitation.section_id)
                    .where(WikiCitation.claim_id.in_(claim_ids))
                    .distinct()
                ).all()
            )[:5]
            if claim_ids
            else []
        )
        graph_store().mark_incident_resolved(db, incident.title, section_ids)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("incident graph registration failed for %s: %s", incident.id, exc)
    return {"incident_id": incident.id, "reused": False}


def feishu_notify(db: Session, args: dict[str, Any]) -> dict[str, Any]:
    receive_id = str(args.get("receive_id") or settings.feishu_default_chat_id or "")
    message = str(args.get("message") or "")
    if not message and args.get("task_id"):
        task = db.get(AgentTask, str(args["task_id"]))
        if task:
            parts = [f"Agent 任务 {task.status.value}：{task.instruction[:80]}"]
            summary = ""
            for record in sorted(task.tool_calls, key=lambda item: item.step_index):
                if record.status != "succeeded":
                    continue
                output = record.output or {}
                candidate = output.get("answer") or output.get("summary")
                if candidate:
                    summary = str(candidate)
            if summary:
                parts.append(summary[:300])
            message = "\n".join(parts)
    if not message:
        message = "Agent 任务已处理完成"
    if not receive_id:
        return {"sent": False, "error": "缺少飞书接收方（receive_id 或 FEISHU_DEFAULT_CHAT_ID）"}
    try:
        data = feishu_client.send_text(receive_id, message)
    except FeishuError as exc:
        return {"sent": False, "error": str(exc)}
    return {"sent": True, "data": data}
