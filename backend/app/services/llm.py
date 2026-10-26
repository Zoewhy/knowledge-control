from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from ..config import settings
from .text import sentence_claims


class DeterministicFakeEmbeddings(Embeddings):
    """无 API Key / mock 模式下的确定性嵌入，保证测试与本地开发可复现。"""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def _vector(self, content: str) -> list[float]:
        digest = hashlib.sha256(content.encode("utf-8")).digest()
        values = [
            ((digest[index % len(digest)] / 255.0) * 2) - 1
            for index in range(self.dimensions)
        ]
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [round(value / norm, 8) for value in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class LLMGateway:
    """知识库 LLM 门面：对外接口不变，内部由 LangChain 模型与链实现。"""

    def __init__(self) -> None:
        self.enabled = bool(settings.bailian_api_key) and not settings.llm_mock_mode
        if self.enabled:
            self._chat = ChatOpenAI(
                model=settings.bailian_chat_model,
                api_key=settings.bailian_api_key,
                base_url=settings.bailian_base_url,
                temperature=0.1,
            )
            self._embeddings: Embeddings = OpenAIEmbeddings(
                model=settings.bailian_embedding_model,
                api_key=settings.bailian_api_key,
                base_url=settings.bailian_base_url,
                check_embedding_ctx_length=False,
            )
            # LCEL: 模型 -> 纯文本解析器
            self._answer_chain = self._chat | StrOutputParser()
        else:
            self._chat = None
            self._embeddings = DeterministicFakeEmbeddings(
                settings.bailian_embedding_dimensions
            )
            self._answer_chain = None

    def structured(self, system: str, user: str, fallback: Any) -> Any:
        if not self._chat:
            return fallback
        response = self._chat.bind(response_format={"type": "json_object"}).invoke(
            [("system", system), ("user", user)]
        )
        raw = (
            response.content
            if isinstance(response.content, str)
            else json.dumps(response.content, ensure_ascii=False)
        )
        return json.loads(raw or "{}")

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
        values = list(self._embeddings.embed_query(content))
        if len(values) < dimensions:
            values.extend([0.0] * (dimensions - len(values)))
        return values[:dimensions]

    def answer(self, query: str, contexts: list[dict[str, Any]]) -> str:
        if not contexts:
            return "知识库中暂未找到足够信息。"
        compact = "\n\n".join(
            f"[{index + 1}] {item['title']}\n{item['content']}"
            for index, item in enumerate(contexts[:6])
        )
        if not self._answer_chain:
            return f"根据知识库，最相关的信息是：{contexts[0]['content'][:320]} [1]"
        return self._answer_chain.invoke(
            [
                SystemMessage(
                    content="仅依据提供的知识回答。每个事实用 [n] 标注来源；证据不足时明确说明。"
                ),
                HumanMessage(content=f"问题：{query}\n\n知识：\n{compact}"),
            ]
        )


llm = LLMGateway()
