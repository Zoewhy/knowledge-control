from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

_CL100K_BLOB_URL = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
_ENCODING = None


def _encoding():
    """加载 cl100k_base；优先使用仓库内置的离线 BPE 资产，避免运行时联网下载。"""
    global _ENCODING
    if _ENCODING is not None:
        return _ENCODING
    import tiktoken

    if not os.environ.get("TIKTOKEN_CACHE_DIR"):
        source = Path(__file__).resolve().parents[1] / "assets" / "cl100k_base.tiktoken"
        if source.exists():
            import shutil
            import tempfile

            cache_dir = Path(tempfile.gettempdir()) / "alg_qa_tiktoken"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_key = hashlib.sha1(_CL100K_BLOB_URL.encode()).hexdigest()
            cached = cache_dir / cache_key
            if not cached.exists():
                shutil.copyfile(source, cached)
            os.environ["TIKTOKEN_CACHE_DIR"] = str(cache_dir)
    _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def count_tokens(text: str) -> int:
    """真实 token 数（cl100k_base）；资产缺失时退化为字符近似，保证管线不中断。"""
    if not text:
        return 0
    try:
        return len(_encoding().encode(text))
    except Exception:
        return max(1, len(text) // 2)


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


MARKDOWN_HEADERS = [
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


def heading_path_from(metadata: dict) -> list[str]:
    return [metadata[key].strip() for _, key in MARKDOWN_HEADERS if metadata.get(key)]


@dataclass
class SegmentDraft:
    heading: Optional[str] = None
    heading_path: list[str] = field(default_factory=list)
    content: str = ""
    segment_type: str = "text"


def split_text_block(
    content: str,
    heading_path: list[str] | None = None,
    max_tokens: int = 512,
    overlap_tokens: int = 50,
) -> list[SegmentDraft]:
    """普通文本块按 token 数二次切分（512 token / 50 token overlap）。"""
    heading_path = list(heading_path or [])
    heading = heading_path[-1] if heading_path else None
    text = content.strip()
    if not text:
        return []
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_tokens,
        chunk_overlap=overlap_tokens,
        length_function=count_tokens,
        separators=["\n\n", "\n", "。", " ", ""],
    )
    chunks = [text] if count_tokens(text) <= max_tokens else splitter.split_text(text)
    return [
        SegmentDraft(heading=heading, heading_path=heading_path, content=chunk.strip())
        for chunk in chunks
        if chunk.strip()
    ]


def split_markdown(
    content: str, max_tokens: int = 512, overlap_tokens: int = 50
) -> list[SegmentDraft]:
    """按 Markdown 标题切分（保留标题路径），超长段落再按 token 二次切分。"""
    header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=MARKDOWN_HEADERS)
    drafts: list[SegmentDraft] = []
    for document in header_splitter.split_text(content):
        heading_path = heading_path_from(document.metadata)
        drafts.extend(
            split_text_block(document.page_content, heading_path, max_tokens, overlap_tokens)
        )
    return drafts or [SegmentDraft(content=content.strip())]


def table_to_markdown(headers: list[str], rows: list[list[str]]) -> str:
    def line(cells: list[str]) -> str:
        cells = [cell.replace("|", "\\|").replace("\n", " ") for cell in cells]
        return "| " + " | ".join(cells) + " |"

    header_line = line(headers)
    separator = "| " + " | ".join("---" for _ in headers) + " |"
    body = [line(row) for row in rows]
    return "\n".join([header_line, separator, *body])


def sentence_claims(content: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", content)
    claims = [part.strip(" -\t") for part in parts if len(part.strip()) >= 8]
    return claims[:20] or [content.strip()]
