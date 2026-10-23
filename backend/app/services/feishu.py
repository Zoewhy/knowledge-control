import httpx

from ..config import settings


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self) -> None:
        self.base_url = settings.feishu_base_url.rstrip("/")

    def _tenant_token(self, client: httpx.Client) -> str:
        if not settings.feishu_app_id or not settings.feishu_app_secret:
            raise FeishuError("飞书应用凭证未配置")
        response = client.post(
            f"{self.base_url}/auth/v3/tenant_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0 or not payload.get("tenant_access_token"):
            raise FeishuError(payload.get("msg") or "获取飞书 tenant token 失败")
        return payload["tenant_access_token"]

    def read_document(self, document_token: str) -> str:
        with httpx.Client(timeout=15) as client:
            token = self._tenant_token(client)
            response = client.get(
                f"{self.base_url}/docx/v1/documents/{document_token}/raw_content",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") != 0:
            raise FeishuError(payload.get("msg") or "读取飞书文档失败")
        content = payload.get("data", {}).get("content", "").strip()
        if not content:
            raise FeishuError("飞书文档为空或当前应用没有读取权限")
        return content


feishu_client = FeishuClient()
