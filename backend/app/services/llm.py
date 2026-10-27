from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import Any, Optional

from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
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

    _ANSWER_SYSTEM_PROMPT = (
        "仅依据提供的知识回答。每个事实用 [n] 标注来源；证据不足时明确说明。"
    )

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
                SystemMessage(content=self._ANSWER_SYSTEM_PROMPT),
                HumanMessage(content=f"问题：{query}\n\n知识：\n{compact}"),
            ]
        )

    def _answer_messages(
        self,
        query: str,
        contexts: list[dict[str, Any]],
        history: list[dict[str, Any]] | None,
    ) -> list:
        compact = "\n\n".join(
            f"[{index + 1}] {item['title']}\n{item['content']}"
            for index, item in enumerate(contexts[:6])
        )
        messages: list = [SystemMessage(content=self._ANSWER_SYSTEM_PROMPT)]
        for item in (history or [])[-6:]:
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            if item.get("role") == "assistant":
                messages.append(AIMessage(content=content))
            else:
                messages.append(HumanMessage(content=content))
        messages.append(HumanMessage(content=f"问题：{query}\n\n知识：\n{compact}"))
        return messages

    def stream_answer(
        self,
        query: str,
        contexts: list[dict[str, Any]],
        history: list[dict[str, Any]] | None = None,
    ):
        """流式回答生成器。mock 模式复用罐头答案并按固定宽度切块，保证确定性、零网络。"""
        if not self._answer_chain:
            canned = self.answer(query, contexts)
            for index in range(0, len(canned), 12):
                yield canned[index : index + 12]
            return
        for chunk in self._answer_chain.stream(
            self._answer_messages(query, contexts, history)
        ):
            text = chunk if isinstance(chunk, str) else str(chunk)
            if text:
                yield text

    def describe_image(self, image_bytes: bytes, hint: Optional[str] = None) -> str:
        """多模态图片描述：提取图中文字与结构。mock 模式返回确定性占位文本。"""
        if not self._chat:
            suffix = f"（{hint}）" if hint else ""
            return f"[图片内容 mock 描述]{suffix}"
        mime = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        vl_chat = ChatOpenAI(
            model=settings.bailian_vl_model,
            api_key=settings.bailian_api_key,
            base_url=settings.bailian_base_url,
            temperature=0.1,
        )
        message = HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": "提取图片中的全部文字与表格/结构信息，用简洁中文描述。"
                    + (f"上下文提示：{hint}" if hint else ""),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
                    },
                },
            ]
        )
        response = vl_chat.invoke([message])
        content = response.content
        return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


llm = LLMGateway()
