from __future__ import annotations

import hashlib
import math
import re
from collections import Counter


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


def split_markdown(content: str, max_chars: int = 1800) -> list[tuple[str | None, str]]:
    blocks: list[tuple[str | None, str]] = []
    heading: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if text:
            blocks.append((heading, text))
        buffer = []

    for line in content.splitlines():
        if re.match(r"^#{1,6}\s+", line):
            flush()
            heading = re.sub(r"^#{1,6}\s+", "", line).strip()
            continue
        if sum(len(item) for item in buffer) + len(line) > max_chars:
            flush()
        buffer.append(line)
    flush()
    return blocks or [(None, content.strip())]


def sentence_claims(content: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", content)
    claims = [part.strip(" -\t") for part in parts if len(part.strip()) >= 8]
    return claims[:20] or [content.strip()]
