from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from openai import OpenAI

from ..config import settings
from .text import sentence_claims


class LLMGateway:
    def __init__(self) -> None:
        self.enabled = bool(settings.bailian_api_key) and not settings.llm_mock_mode
        self.client = (
            OpenAI(api_key=settings.bailian_api_key, base_url=settings.bailian_base_url)
            if self.enabled
            else None
        )

    def structured(self, system: str, user: str, fallback: Any) -> Any:
        if not self.client:
            return fallback
        response = self.client.chat.completions.create(
            model=settings.bailian_chat_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        raw = response.choices[0].message.content or "{}"
        return json.loads(raw)

    def extract_claims(self, content: str, heading: str | None) -> list[dict[str, Any]]:
        fallback = {
            "claims": [
                {"statement": statement, "scope": heading or "general", "confidence": 0.75}
                for statement in sentence_claims(content)
            ]
        }
        result = self.structured(
            "你是算法质量知识工程师。将原文拆成原子、可独立验证的知识声明。"
            "只返回 JSON：{claims:[{statement,scope,confidence}]}，禁止补充原文没有的信息。",
            f"标题：{heading or '无'}\n原文：\n{content}",
            fallback,
        )
        return result.get("claims", fallback["claims"])

    def embed(self, content: str) -> list[float]:
        dimensions = settings.bailian_embedding_dimensions
        if self.client:
            response = self.client.embeddings.create(
                model=settings.bailian_embedding_model,
                input=content,
            )
            values = list(response.data[0].embedding)
            if len(values) < dimensions:
                values.extend([0.0] * (dimensions - len(values)))
            return values[:dimensions]
        digest = hashlib.sha256(content.encode("utf-8")).digest()
        values = [((digest[index % len(digest)] / 255.0) * 2) - 1 for index in range(dimensions)]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [round(value / norm, 8) for value in values]

    def answer(self, query: str, contexts: list[dict[str, Any]]) -> str:
        if not contexts:
            return "知识库中暂未找到足够信息。"
        compact = "\n\n".join(
            f"[{index + 1}] {item['title']}\n{item['content']}"
            for index, item in enumerate(contexts[:6])
        )
        if not self.client:
            return f"根据知识库，最相关的信息是：{contexts[0]['content'][:320]} [1]"
        response = self.client.chat.completions.create(
            model=settings.bailian_chat_model,
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "仅依据提供的知识回答。每个事实用 [n] 标注来源；证据不足时明确说明。"
                    ),
                },
                {"role": "user", "content": f"问题：{query}\n\n知识：\n{compact}"},
            ],
        )
        return response.choices[0].message.content or ""


llm = LLMGateway()
