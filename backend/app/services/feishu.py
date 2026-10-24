import json
import time
from typing import Any, Optional

import httpx

from ..config import settings


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self) -> None:
        self.base_url = settings.feishu_base_url.rstrip("/")
        self._token: Optional[str] = None
        self._token_expire_at: float = 0.0

    def _tenant_token(self, client: httpx.Client) -> str:
        if not settings.feishu_app_id or not settings.feishu_app_secret:
            raise FeishuError("飞书应用凭证未配置")
        if self._token and time.time() < self._token_expire_at:
            return self._token
        response = client.post(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        response.raise_for_status()
        payload = self._check(response.json(), "获取飞书 tenant token 失败")
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuError("获取飞书 tenant token 失败")
        expire = int(payload.get("expire", 7200))
        self._token = token
        self._token_expire_at = time.time() + max(expire - 120, 60)
        return token

    @staticmethod
    def _check(payload: dict[str, Any], default_message: str) -> dict[str, Any]:
        if payload.get("code") != 0:
            raise FeishuError(payload.get("msg") or default_message)
        return payload

    def read_document(self, document_token: str) -> str:
        with httpx.Client(timeout=15) as client:
            token = self._tenant_token(client)
            response = client.get(
                f"{self.base_url}/docx/v1/documents/{document_token}/raw_content",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = self._check(response.json(), "读取飞书文档失败")
        content = payload.get("data", {}).get("content", "").strip()
        if not content:
            raise FeishuError("飞书文档为空或当前应用没有读取权限")
        return content

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        if not receive_id:
            raise FeishuError("缺少飞书接收方 ID")
        with httpx.Client(timeout=10) as client:
            token = self._tenant_token(client)
            response = client.post(
                f"{self.base_url}/im/v1/messages",
                params={"receive_id_type": receive_id_type},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
            response.raise_for_status()
            payload = self._check(response.json(), "发送飞书消息失败")
        return payload.get("data", {})

    def send_task_result(self, chat_id: str, task: Any) -> dict:
        status_text = getattr(task.status, "value", task.status)
        lines = [f"Agent 任务 {status_text}", f"指令：{task.instruction[:120]}"]
        if task.error:
            lines.append(f"错误：{task.error[:200]}")
        final = (task.result or {}).get("final") or {}
        if isinstance(final, dict):
            answer = final.get("answer") or final.get("summary")
            if answer:
                lines.append(f"结果：{str(answer)[:400]}")
        return self.send_text(chat_id, "\n".join(lines))

    def send_review_notification(self, chat_id: str, review_task: Any) -> dict:
        lines = [
            "知识审核任务待处理",
            f"原因：{review_task.reason[:200]}",
            f"动作：{getattr(review_task.action, 'value', review_task.action)}",
            f"审核任务 ID：{review_task.id}",
            "请在 Review 工作台处理，或回调 value 携带 review_task_id 与 decision。",
        ]
        return self.send_text(chat_id, "\n".join(lines))


feishu_client = FeishuClient()
