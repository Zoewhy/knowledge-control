from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AlertEvent, IntegrationEvent, QaRequirement, utcnow
from ..schemas import DocumentCreate
from .agent_runtime import create_task
from .feishu import FeishuError, feishu_client
from .ingestion import compile_document, ingest_document
from .scheduler import dispatch_task

QA_PREFIXES = ("问答:", "问答：")
CASE_PREFIXES = ("用例:", "用例：")
ALERT_PREFIXES = ("归因:", "归因：")
DOC_PREFIXES = ("文档 ", "文档　")


class IntegrationError(RuntimeError):
    pass


def fingerprint_alert(source: str, title: str, service: str, labels: dict[str, Any]) -> str:
    normalized = json.dumps(labels or {}, sort_keys=True, ensure_ascii=False)
    raw = f"{source}|{title.strip().lower()}|{service.strip().lower()}|{normalized}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _record_event(
    db: Session,
    provider: str,
    event_type: str,
    payload: dict[str, Any],
    dedupe_key: str,
    event_id: Optional[str] = None,
) -> tuple[IntegrationEvent, bool]:
    existing = db.scalar(
        select(IntegrationEvent).where(IntegrationEvent.dedupe_key == dedupe_key)
    )
    if existing:
        return existing, False
    event = IntegrationEvent(
        provider=provider,
        event_id=event_id,
        event_type=event_type,
        dedupe_key=dedupe_key,
        payload=payload,
        status="received",
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event, True


def verify_feishu_token(token: Optional[str]) -> bool:
    configured = settings.feishu_verification_token
    if not configured:
        return True
    return bool(token) and token == configured


def _extract_message_text(event_body: dict[str, Any]) -> Optional[str]:
    message = event_body.get("message") or {}
    if message.get("message_type") != "text":
        return None
    try:
        content = json.loads(message.get("content") or "{}")
    except (TypeError, ValueError):
        return None
    text = (content.get("text") or "").strip()
    # @机器人 时飞书会带 @_user_1 占位符
    for part in text.split():
        if part.startswith("@_user_"):
            text = text.replace(part, "")
    return text.strip() or None


def _dispatch(db: Session, event: IntegrationEvent, instruction: str, context: dict[str, Any]):
    context = {**context, "integration_event_id": event.id}
    task = create_task(db, instruction, context)
    event.agent_task_id = task.id
    db.commit()
    task = dispatch_task(db, task)
    return task


def _finish_event(
    db: Session, event: IntegrationEvent, status: str, error: Optional[str] = None
) -> None:
    event.status = status
    event.error = error
    event.processed_at = utcnow()
    db.commit()


def handle_feishu_event(db: Session, body: dict[str, Any]) -> dict[str, Any]:
    if "encrypt" in body:
        raise IntegrationError("当前版本未实现加密事件解密，请使用明文事件订阅")

    # URL 验证
    if body.get("type") == "url_verification":
        if not verify_feishu_token(body.get("token")):
            raise PermissionError("飞书 verification token 校验失败")
        return {"challenge": body.get("challenge")}

    header = body.get("header") or {}
    token = header.get("token") or body.get("token")
    if not verify_feishu_token(token):
        raise PermissionError("飞书 verification token 校验失败")

    event_id = header.get("event_id") or body.get("event_id")
    event_type = header.get("event_type") or body.get("event_type") or "unknown"
    event_body = body.get("event") or {}
    dedupe_key = f"feishu:{event_id}" if event_id else (
        "feishu:anon:"
        + hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    )
    event, created = _record_event(db, "feishu", event_type, body, dedupe_key, event_id)
    if not created:
        return {"status": "duplicate", "integration_event_id": event.id}

    result: dict[str, Any] = {"status": "processed", "integration_event_id": event.id}
    try:
        routed = _route_feishu_event(db, event, event_type, event_body)
        result.update(routed)
        if routed.get("status") == "processed":
            _finish_event(db, event, "processed")
    except (FeishuError, ValueError) as exc:
        _finish_event(db, event, "failed", str(exc))
        result = {"status": "failed", "integration_event_id": event.id, "error": str(exc)}
    return result


def _route_feishu_event(
    db: Session, event: IntegrationEvent, event_type: str, event_body: dict[str, Any]
) -> dict[str, Any]:
    if event_type == "im.message.receive_v1":
        return _handle_message(db, event, event_body)
    if "doc" in event_type or "drive" in event_type:
        token = event_body.get("file_token") or event_body.get("token")
        if not token:
            _finish_event(db, event, "ignored", "文档事件缺少 file_token")
            return {"status": "ignored", "reason": "文档事件缺少 file_token"}
        content = feishu_client.read_document(str(token))
        document, doc_created = ingest_document(
            db,
            DocumentCreate(
                title=f"飞书文档 {token}",
                content=content,
                source_type="feishu",
                source_uri=f"feishu://docx/{token}",
                owner="feishu-event",
                metadata={"document_token": token, "integration_event_id": event.id},
                compile=True,
            ),
        )
        if doc_created:
            compile_document(db, document)
        return {"status": "processed", "document_id": document.id, "document_created": doc_created}
    _finish_event(db, event, "ignored", f"未支持的事件类型 {event_type}")
    return {"status": "ignored", "reason": f"未支持的事件类型 {event_type}"}


def _handle_message(
    db: Session, event: IntegrationEvent, event_body: dict[str, Any]
) -> dict[str, Any]:
    text = _extract_message_text(event_body)
    if not text:
        _finish_event(db, event, "ignored", "仅支持文本消息")
        return {"status": "ignored", "reason": "仅支持文本消息"}
    message = event_body.get("message") or {}
    sender = (event_body.get("sender") or {}).get("sender_id") or {}
    context = {
        "chat_id": message.get("chat_id"),
        "message_id": message.get("message_id"),
        "sender_id": sender.get("open_id"),
    }

    for prefix in DOC_PREFIXES:
        if text.startswith(prefix):
            token = text[len(prefix):].strip()
            if not token:
                raise ValueError("缺少文档 token")
            content = feishu_client.read_document(token)
            document, doc_created = ingest_document(
                db,
                DocumentCreate(
                    title=f"飞书文档 {token}",
                    content=content,
                    source_type="feishu",
                    source_uri=f"feishu://docx/{token}",
                    owner=sender.get("open_id") or "feishu",
                    metadata={"document_token": token, "integration_event_id": event.id},
                    compile=True,
                ),
            )
            if doc_created:
                compile_document(db, document)
            return {
                "status": "processed",
                "document_id": document.id,
                "document_created": doc_created,
            }

    for prefix in QA_PREFIXES:
        if text.startswith(prefix):
            question = text[len(prefix):].strip()
            task = _dispatch(
                db, event, question, {**context, "task_type": "feishu_qa", "question": question}
            )
            return {"status": "processed", "agent_task_id": task.id, "route": "qa"}

    for prefix in CASE_PREFIXES:
        if text.startswith(prefix):
            content = text[len(prefix):].strip()
            title = content.splitlines()[0][:60] if content else "飞书需求"
            requirement = QaRequirement(
                title=title or "飞书需求",
                content=content,
                metadata_json={"source": "feishu", "integration_event_id": event.id},
            )
            db.add(requirement)
            db.commit()
            task = _dispatch(
                db,
                event,
                f"为「{requirement.title}」生成测试用例",
                {
                    **context,
                    "task_type": "qa_generate",
                    "requirement_id": requirement.id,
                    "query": requirement.title,
                },
            )
            return {
                "status": "processed",
                "agent_task_id": task.id,
                "requirement_id": requirement.id,
                "route": "qa_generate",
            }

    for prefix in ALERT_PREFIXES:
        if text.startswith(prefix):
            description = text[len(prefix):].strip()
            title = description.splitlines()[0][:120] if description else "飞书告警归因"
            fingerprint = hashlib.sha256(
                f"feishu:{context.get('message_id') or event.dedupe_key}".encode("utf-8")
            ).hexdigest()
            alert = db.scalar(select(AlertEvent).where(AlertEvent.fingerprint == fingerprint))
            if not alert:
                alert = AlertEvent(
                    fingerprint=fingerprint,
                    source="feishu",
                    title=title,
                    service="unknown",
                    severity="P2",
                    description=description,
                    labels={"chat_id": context.get("chat_id") or ""},
                )
                db.add(alert)
                db.commit()
                db.refresh(alert)
            task = _dispatch(
                db,
                event,
                f"告警归因：{alert.title}",
                {
                    **context,
                    "task_type": "alert_triage",
                    "alert_id": alert.id,
                    "query": f"{alert.title} {alert.service}".strip(),
                },
            )
            return {
                "status": "processed",
                "agent_task_id": task.id,
                "alert_id": alert.id,
                "route": "alert_triage",
            }

    _finish_event(db, event, "ignored", "消息未匹配指令前缀（文档/问答:/用例:/归因:）")
    return {"status": "ignored", "reason": "消息未匹配指令前缀（文档/问答:/用例:/归因:）"}


def ingest_alert(
    db: Session,
    *,
    title: str,
    source: str,
    service: str,
    severity: str,
    description: str,
    labels: dict[str, Any],
    fingerprint: Optional[str],
    auto_triage: bool,
) -> tuple[AlertEvent, bool, Optional[str]]:
    fingerprint = fingerprint or fingerprint_alert(source, title, service, labels)
    alert = db.scalar(select(AlertEvent).where(AlertEvent.fingerprint == fingerprint))
    if alert:
        alert.occurrence_count += 1
        alert.updated_at = utcnow()
        db.commit()
        return alert, False, alert.agent_task_id
    alert = AlertEvent(
        fingerprint=fingerprint,
        source=source,
        title=title,
        service=service,
        severity=severity,
        description=description,
        labels=labels,
        payload={"title": title, "source": source, "labels": labels},
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    task_id = None
    if auto_triage:
        task_id = create_alert_triage_task(db, alert)
    return alert, True, task_id


def create_alert_triage_task(db: Session, alert: AlertEvent) -> str:
    task = create_task(
        db,
        f"告警归因：{alert.title}",
        {
            "task_type": "alert_triage",
            "alert_id": alert.id,
            "query": f"{alert.title} {alert.service}".strip(),
        },
    )
    alert.agent_task_id = task.id
    db.commit()
    task = dispatch_task(db, task)
    return task.id
