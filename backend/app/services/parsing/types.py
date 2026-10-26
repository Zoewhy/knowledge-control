from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass
class TextBlock:
    heading_path: list[str]
    content: str


@dataclass
class TableBlock:
    heading_path: list[str]
    headers: list[str]
    rows: list[list[str]]


@dataclass
class ImageBlock:
    heading_path: list[str]
    image_bytes: bytes
    caption: Optional[str] = None


Block = Union[TextBlock, TableBlock, ImageBlock]


@dataclass
class ParsedDoc:
    blocks: list[Block] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
