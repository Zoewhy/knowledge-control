"""切分策略注册表：统一 ChunkerFn(blocks, params) -> list[SegmentDraft]。

blocks 为解析层序列化后的 dict 块（text/table/image），新增策略 = 注册一个函数。
"""

from __future__ import annotations

import re
from typing import Callable

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .text import (
    SegmentDraft,
    count_tokens,
    split_text_block,
    table_to_markdown,
)

ChunkerFn = Callable[[list[dict], dict], list[SegmentDraft]]

DEFAULT_MAX_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 50
_PAGE_MARKER = re.compile(r"^第\s*\d+\s*页$")


def _max_tokens(params: dict) -> int:
    return int(params.get("max_tokens", DEFAULT_MAX_TOKENS))


def _overlap_tokens(params: dict) -> int:
    return int(params.get("overlap_tokens", DEFAULT_OVERLAP_TOKENS))


def _block_text(block: dict) -> str:
    return (block.get("content") or block.get("description") or "").strip()


def _block_heading(block: dict) -> tuple[list[str], str | None]:
    heading_path = list(block.get("heading_path") or [])
    return heading_path, (heading_path[-1] if heading_path else None)


def _table_draft(block: dict, rows: list[list[str]] | None = None) -> SegmentDraft:
    heading_path, heading = _block_heading(block)
    content = table_to_markdown(block["headers"], rows if rows is not None else block["rows"])
    return SegmentDraft(
        heading=heading, heading_path=heading_path, content=content, segment_type="table"
    )


def heading_chunker(blocks: list[dict], params: dict) -> list[SegmentDraft]:
    """现状行为：表格整块成段，文本/图片保留 heading_path 并按 token 二次切分。"""
    drafts: list[SegmentDraft] = []
    for block in blocks:
        heading_path, _ = _block_heading(block)
        if block["type"] == "table":
            drafts.append(_table_draft(block))
            continue
        segment_type = "image" if block["type"] == "image" else "text"
        for draft in split_text_block(
            _block_text(block), heading_path, _max_tokens(params), _overlap_tokens(params)
        ):
            draft.segment_type = segment_type
            drafts.append(draft)
    return [draft for draft in drafts if draft.content.strip()]


def fixed_chunker(blocks: list[dict], params: dict) -> list[SegmentDraft]:
    """忽略标题与块边界，全文拼合后按固定 token 窗口 + overlap 切分。"""
    parts: list[str] = []
    for block in blocks:
        if block["type"] == "table":
            parts.append(table_to_markdown(block["headers"], block["rows"]))
        else:
            parts.append(_block_text(block))
    text = "\n\n".join(part for part in parts if part)
    return split_text_block(text, [], _max_tokens(params), _overlap_tokens(params))


def recursive_chunker(blocks: list[dict], params: dict) -> list[SegmentDraft]:
    """相邻同标题块合并后按递归分隔符（段落→句子）切分，保留语义边界；表格整块。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=_max_tokens(params),
        chunk_overlap=_overlap_tokens(params),
        length_function=count_tokens,
        separators=["\n\n", "\n", "。", "；", " ", ""],
    )
    drafts: list[SegmentDraft] = []
    pending_path: tuple[str, ...] | None = None
    pending_texts: list[str] = []

    def flush() -> None:
        nonlocal pending_path, pending_texts
        if not pending_texts:
            return
        heading_path = list(pending_path or [])
        heading = heading_path[-1] if heading_path else None
        content = "\n\n".join(pending_texts)
        for chunk in splitter.split_text(content):
            if chunk.strip():
                drafts.append(
                    SegmentDraft(heading=heading, heading_path=heading_path, content=chunk.strip())
                )
        pending_path, pending_texts = None, []

    for block in blocks:
        if block["type"] == "table":
            flush()
            drafts.append(_table_draft(block))
            continue
        text = _block_text(block)
        if not text:
            continue
        heading_path, _ = _block_heading(block)
        key = tuple(heading_path)
        if pending_path is not None and key != pending_path:
            flush()
        pending_path = key
        pending_texts.append(text)
    flush()
    return drafts


def page_chunker(blocks: list[dict], params: dict) -> list[SegmentDraft]:
    """按 heading_path 第一层页标记（第 N 页）归并成段，超长按 fixed 兜底。"""
    groups: list[tuple[list[str], list[dict]]] = []
    for block in blocks:
        heading_path, _ = _block_heading(block)
        page = heading_path[0] if heading_path and _PAGE_MARKER.match(heading_path[0]) else None
        if page and groups and groups[-1][0] == [page]:
            groups[-1][1].append(block)
        elif page:
            groups.append(([page], [block]))
        else:
            groups.append((heading_path, [block]))

    drafts: list[SegmentDraft] = []
    for heading_path, group_blocks in groups:
        heading = heading_path[-1] if heading_path else None
        parts = []
        for block in group_blocks:
            if block["type"] == "table":
                parts.append(table_to_markdown(block["headers"], block["rows"]))
            else:
                parts.append(_block_text(block))
        content = "\n\n".join(part for part in parts if part).strip()
        if not content:
            continue
        if count_tokens(content) <= _max_tokens(params):
            drafts.append(
                SegmentDraft(heading=heading, heading_path=heading_path, content=content)
            )
            continue
        drafts.extend(
            split_text_block(content, heading_path, _max_tokens(params), _overlap_tokens(params))
        )
    return drafts


def table_row_chunker(blocks: list[dict], params: dict) -> list[SegmentDraft]:
    """大表按行成段（每段注入表头），小表仍整块；非表格块同 heading 策略。"""
    rows_per_segment = int(params.get("rows_per_segment", 20))
    small_table_rows = int(params.get("small_table_rows", 20))
    drafts: list[SegmentDraft] = []
    for block in blocks:
        if block["type"] != "table":
            drafts.extend(heading_chunker([block], params))
            continue
        rows = [list(row) for row in block["rows"]]
        if len(rows) <= small_table_rows:
            drafts.append(_table_draft(block))
            continue
        for start in range(0, len(rows), rows_per_segment):
            drafts.append(_table_draft(block, rows[start : start + rows_per_segment]))
    return [draft for draft in drafts if draft.content.strip()]


CHUNKERS: dict[str, ChunkerFn] = {
    "heading": heading_chunker,
    "fixed": fixed_chunker,
    "recursive": recursive_chunker,
    "page": page_chunker,
    "table_row": table_row_chunker,
}


def get_chunker(name: str) -> ChunkerFn:
    chunker = CHUNKERS.get(name or "")
    if chunker is None:
        raise ValueError(f"未知切分策略: {name}（可选: {', '.join(sorted(CHUNKERS))}）")
    return chunker
