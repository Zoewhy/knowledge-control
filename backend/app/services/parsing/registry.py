from __future__ import annotations

import re
from io import BytesIO

from langchain_text_splitters import MarkdownHeaderTextSplitter

from ..text import MARKDOWN_HEADERS
from .types import ImageBlock, ParsedDoc, TableBlock, TextBlock

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class UnsupportedDocumentError(ValueError):
    pass


def _heading_path(metadata: dict) -> list[str]:
    return [metadata[key].strip() for _, key in MARKDOWN_HEADERS if metadata.get(key)]


def parse_text_document(filename: str, raw: bytes) -> ParsedDoc:
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedDocumentError("文本文件必须使用 UTF-8 编码") from exc
    if not content.strip():
        raise UnsupportedDocumentError("文件内容不能为空")
    splitter = MarkdownHeaderTextSplitter(headers_to_split_on=MARKDOWN_HEADERS)
    blocks = [
        TextBlock(heading_path=_heading_path(document.metadata), content=document.page_content)
        for document in splitter.split_text(content)
        if document.page_content.strip()
    ]
    return ParsedDoc(
        blocks=blocks or [TextBlock(heading_path=[], content=content)],
        meta={"mime": "text", "text_chars": len(content)},
    )


def parse_pdf_document(filename: str, raw: bytes) -> ParsedDoc:
    import pymupdf

    try:
        pdf = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise UnsupportedDocumentError(f"PDF 文件无法解析: {exc}") from exc
    blocks: list = []
    text_chars = 0
    scanned_pages: list[int] = []
    page_count = pdf.page_count
    try:
        for page in pdf:
            page_no = page.number + 1
            heading_path = [f"第 {page_no} 页"]
            page_text = page.get_text().strip()
            if not page_text:
                scanned_pages.append(page_no)
                image = page.get_pixmap(dpi=150).tobytes("png")
                blocks.append(ImageBlock(heading_path=heading_path, image_bytes=image))
                continue
            text_chars += len(page_text)
            items: list[tuple[float, str, object]] = [
                (float(block[1]), "text", str(block[4]))
                for block in page.get_text("blocks")
                if len(block) >= 5 and str(block[4]).strip()
            ]
            try:
                for table in page.find_tables():
                    rows = [[str(cell or "").strip() for cell in row] for row in table.extract()]
                    if rows:
                        items.append((float(table.bbox[1]), "table", rows))
            except Exception:
                pass
            buffer: list[str] = []
            for _, kind, payload in sorted(items, key=lambda item: item[0]):
                if kind == "text":
                    buffer.append(str(payload).strip())
                    continue
                if buffer:
                    blocks.append(TextBlock(heading_path=heading_path, content="\n\n".join(buffer)))
                    buffer = []
                rows = payload
                blocks.append(
                    TableBlock(heading_path=heading_path, headers=rows[0], rows=rows[1:])
                )
            if buffer:
                blocks.append(TextBlock(heading_path=heading_path, content="\n\n".join(buffer)))
    finally:
        pdf.close()
    return ParsedDoc(
        blocks=blocks,
        meta={
            "mime": "application/pdf",
            "pages": page_count,
            "text_chars": text_chars,
            "scanned_pages": scanned_pages,
        },
    )


_HEADING_STYLE = re.compile(r"^(Heading|标题)\s*(\d+)$", re.IGNORECASE)


def parse_docx_document(filename: str, raw: bytes) -> ParsedDoc:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(BytesIO(raw))
    except Exception as exc:
        raise UnsupportedDocumentError(f"DOCX 文件无法解析: {exc}") from exc
    blocks: list = []
    headings: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if buffer:
            blocks.append(TextBlock(heading_path=list(headings), content="\n\n".join(buffer)))
            buffer = []

    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            match = _HEADING_STYLE.match(paragraph.style.name or "")
            if match:
                flush()
                depth = int(match.group(2))
                headings = headings[: depth - 1] + [text]
            else:
                buffer.append(text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            flush()
            blocks.append(
                TableBlock(heading_path=list(headings), headers=rows[0], rows=rows[1:])
            )
    flush()
    return ParsedDoc(blocks=blocks, meta={"mime": DOCX_MIME})


def parse_xlsx_document(filename: str, raw: bytes) -> ParsedDoc:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise UnsupportedDocumentError(f"XLSX 文件无法解析: {exc}") from exc
    blocks: list = []
    try:
        for sheet in workbook.worksheets:
            rows = [
                ["" if cell is None else str(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            rows = [row for row in rows if any(cell.strip() for cell in row)]
            if not rows:
                continue
            blocks.append(
                TableBlock(heading_path=[sheet.title], headers=rows[0], rows=rows[1:])
            )
    finally:
        workbook.close()
    return ParsedDoc(blocks=blocks, meta={"mime": XLSX_MIME})


_MIME_PARSERS = {
    "text/markdown": parse_text_document,
    "text/plain": parse_text_document,
    "application/pdf": parse_pdf_document,
    DOCX_MIME: parse_docx_document,
    XLSX_MIME: parse_xlsx_document,
}

_IMAGE_MIMES = {"image/png", "image/jpeg"}


def parse_document(filename: str, content_bytes: bytes, mime: str) -> ParsedDoc:
    """按 MIME 分发解析，返回保序的块序列。"""
    if mime in _IMAGE_MIMES:
        return ParsedDoc(
            blocks=[ImageBlock(heading_path=[], image_bytes=content_bytes)],
            meta={"mime": mime},
        )
    parser = _MIME_PARSERS.get(mime)
    if not parser:
        raise UnsupportedDocumentError(f"不支持的文件类型: {mime}")
    return parser(filename, content_bytes)
