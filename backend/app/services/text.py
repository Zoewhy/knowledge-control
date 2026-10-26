from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)


def fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def slugify(text: str) -> str:
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text.strip().lower()).strip("-")
    return value[:160] or fingerprint(text)[:12]


def tokenize(text: str) -> list[str]:
    latin = re.findall(r"[a-zA-Z0-9_]{2,}", text.lower())
    chinese: list[str] = []
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(phrase) == 1:
            chinese.append(phrase)
        else:
            chinese.extend(phrase[index : index + 2] for index in range(len(phrase) - 1))
    return latin + chinese


def lexical_score(query: str, content: str) -> float:
    query_tokens = Counter(tokenize(query))
    content_tokens = Counter(tokenize(content))
    if not query_tokens or not content_tokens:
        return 0.0
    overlap = sum(min(value, content_tokens[token]) for token, value in query_tokens.items())
    norm = math.sqrt(sum(query_tokens.values()) * sum(content_tokens.values()))
    return round(overlap / norm, 6) if norm else 0.0


_MARKDOWN_HEADERS = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
    ("####", "h4"),
    ("#####", "h5"),
    ("######", "h6"),
]


def _deepest_heading(metadata: dict) -> str | None:
    for level in ("h6", "h5", "h4", "h3", "h2", "h1"):
        value = metadata.get(level)
        if value:
            return value.strip() or None
    return None


def split_markdown(content: str, max_chars: int = 1800) -> list[tuple[str | None, str]]:
    """按 Markdown 标题切分（LangChain splitter），超长段落再做二次切分。"""
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=_MARKDOWN_HEADERS)
    length_splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chars,
        chunk_overlap=0,
        separators=["\n\n", "\n", "。", " ", ""],
    )
    blocks: list[tuple[str | None, str]] = []
    for document in header_splitter.split_text(content):
        heading = _deepest_heading(document.metadata)
        text = document.page_content.strip()
        if not text:
            continue
        if len(text) <= max_chars:
            blocks.append((heading, text))
            continue
        for chunk in length_splitter.split_text(text):
            chunk = chunk.strip()
            if chunk:
                blocks.append((heading, chunk))
    return blocks or [(None, content.strip())]


def sentence_claims(content: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", content)
    claims = [part.strip(" -\t") for part in parts if len(part.strip()) >= 8]
    return claims[:20] or [content.strip()]
