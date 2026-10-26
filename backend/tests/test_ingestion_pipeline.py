import base64
import io

import pytest
from app.config import settings
from app.database import SessionLocal
from app.models import DocumentFile, SourceDocument, SourceSegment
from app.services.ingestion import run_ingest_pipeline
from app.services.parsing import parse_document
from app.services.parsing.types import ImageBlock, TableBlock, TextBlock
from app.services.text import count_tokens

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
    "AAAABJRU5ErkJggg=="
)

PDF_TEXT = (
    "商家入驻审核需要校验营业执照主体名称、统一社会信用代码与经营范围，"
    "OCR 置信度低于阈值时必须进入人工复核，不允许自动通过。"
)


def make_pdf(scanned: bool = True) -> bytes:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 300), PDF_TEXT, fontname="china-s")
    if scanned:
        document.new_page()
    raw = document.tobytes()
    document.close()
    return raw


def make_low_text_pdf() -> bytes:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "太短", fontname="china-s")
    raw = document.tobytes()
    document.close()
    return raw


def make_docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("授权审核", level=1)
    document.add_paragraph("品牌授权需要核验授权方与被授权方主体一致性。")
    document.add_heading("授权链路", level=2)
    document.add_paragraph("多级授权必须逐级收窄，下游范围不得超过上游。")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "驳回码"
    sheet.append(["驳回码", "说明"])
    sheet.append(["A001", "缺少品牌授权书"])
    sheet.append(["A002", "授权链路断裂"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def upload(client, filename: str, raw: bytes, mime: str, compile: bool = True):
    return client.post(
        "/api/v1/documents/upload",
        files={"file": (filename, io.BytesIO(raw), mime)},
        data={"owner": "qa", "compile": str(compile).lower()},
    )


def test_parse_markdown_heading_path():
    parsed = parse_document(
        "rule.md", "# 总则\n概述文本。\n## 细则\n细则内容文本。".encode(), "text/markdown"
    )
    paths = [block.heading_path for block in parsed.blocks]
    assert ["总则"] in paths
    assert ["总则", "细则"] in paths
    assert all(isinstance(block, TextBlock) for block in parsed.blocks)


def test_parse_pdf_blocks_and_scanned_page():
    parsed = parse_document("rule.pdf", make_pdf(), "application/pdf")
    assert parsed.meta["pages"] == 2
    assert parsed.meta["scanned_pages"] == [2]
    assert parsed.meta["text_chars"] >= len(PDF_TEXT)
    text_blocks = [block for block in parsed.blocks if isinstance(block, TextBlock)]
    image_blocks = [block for block in parsed.blocks if isinstance(block, ImageBlock)]
    assert text_blocks and "人工复核" in text_blocks[0].content
    assert text_blocks[0].heading_path == ["第 1 页"]
    assert len(image_blocks) == 1
    assert image_blocks[0].heading_path == ["第 2 页"]


def test_parse_docx_heading_hierarchy():
    parsed = parse_document("rule.docx", make_docx(), DOCX_MIME)
    paths = [block.heading_path for block in parsed.blocks]
    assert ["授权审核"] in paths
    assert ["授权审核", "授权链路"] in paths


def test_parse_xlsx_table_blocks():
    parsed = parse_document("codes.xlsx", make_xlsx(), XLSX_MIME)
    assert len(parsed.blocks) == 1
    block = parsed.blocks[0]
    assert isinstance(block, TableBlock)
    assert block.heading_path == ["驳回码"]
    assert block.headers == ["驳回码", "说明"]
    assert ["A001", "缺少品牌授权书"] in block.rows


def test_parse_image_block():
    parsed = parse_document("seal.png", PNG_BYTES, "image/png")
    assert len(parsed.blocks) == 1
    assert isinstance(parsed.blocks[0], ImageBlock)
    assert parsed.blocks[0].image_bytes == PNG_BYTES


def test_mime_spoofing_rejected(client):
    response = upload(client, "fake.pdf", "这不是 PDF 文件".encode(), "application/pdf")
    assert response.status_code == 422


def test_unsupported_extension_rejected(client):
    response = upload(client, "evil.exe", PNG_BYTES, "image/png")
    assert response.status_code == 415


def test_upload_pdf_sync_processed(client):
    response = upload(client, "rule.pdf", make_pdf(), "application/pdf")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "processed"
    assert document["progress"] == 100
    segments = document["segments"]
    assert segments
    assert segments[0]["heading_path"] == ["第 1 页"]
    assert any("[图片内容 mock 描述]" in segment["content"] for segment in segments)
    with SessionLocal() as db:
        rows = db.query(SourceSegment).filter_by(document_id=document["id"]).all()
        assert all(row.token_count == count_tokens(row.content) for row in rows)
        assert all(row.embedding for row in rows)
        assert all(row.claims for row in rows)
        stored = db.query(DocumentFile).filter_by(document_id=document["id"]).one()
        assert stored.mime == "application/pdf"


def test_upload_xlsx_table_segment(client):
    response = upload(client, "codes.xlsx", make_xlsx(), XLSX_MIME, compile=False)
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "processed"
    assert document["segments"][0]["segment_type"] == "table"
    assert "| 驳回码 | 说明 |" in document["segments"][0]["content"]


def test_low_text_pdf_processed_with_warning(client):
    response = upload(client, "thin.pdf", make_low_text_pdf(), "application/pdf")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "processed"
    assert document["error_message"] is None
    warnings = (document.get("metadata") or {}).get("parse_warnings", [])
    assert any("文本量较少" in warning for warning in warnings)


def test_pipeline_failure_and_retry_resume(client, monkeypatch):
    from app.services import ingestion
    from app.services.llm import llm

    parse_calls = 0
    real_parse = ingestion.parse_document

    def counting_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(ingestion, "parse_document", counting_parse)

    def broken_extract(*args, **kwargs):
        raise RuntimeError("LLM 抖动")

    monkeypatch.setattr(llm, "extract_claims", broken_extract)
    response = upload(client, "rule.pdf", make_pdf(), "application/pdf")
    document = response.json()
    assert document["status"] == "failed"
    assert "extracting" in document["error_message"]
    assert parse_calls == 1

    with SessionLocal() as db:
        segment_count = (
            db.query(SourceSegment).filter_by(document_id=document["id"]).count()
        )
    assert segment_count > 0

    monkeypatch.undo()
    monkeypatch.setattr(ingestion, "parse_document", counting_parse)
    retry = client.post(f"/api/v1/documents/{document['id']}/retry")
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "processed"
    assert retry.json()["error_message"] is None
    # 断点续跑：parsing 不重复执行，segment 不重复创建
    assert parse_calls == 1
    with SessionLocal() as db:
        current = db.query(SourceSegment).filter_by(document_id=document["id"]).count()
        assert current == segment_count


def test_retry_requires_failed_status(client):
    response = upload(client, "rule.pdf", make_pdf(), "application/pdf")
    document = response.json()
    assert document["status"] == "processed"
    retry = client.post(f"/api/v1/documents/{document['id']}/retry")
    assert retry.status_code == 409


def test_run_ingest_pipeline_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path / "files"))
    raw = make_pdf()
    with SessionLocal() as db:
        document = SourceDocument(
            title="幂等.pdf",
            source_type="file_upload",
            checksum="idem-1",
            status="pending",
            metadata_json={"compile": False},
        )
        db.add(document)
        db.flush()
        from app.services.ingestion import store_upload

        stored = DocumentFile(
            document_id=document.id,
            filename="幂等.pdf",
            mime="application/pdf",
            size_bytes=len(raw),
            sha256="idem-1",
            storage_path=store_upload("幂等.pdf", raw, "idem-1"),
        )
        db.add(stored)
        db.flush()
        document.file_id = stored.id
        db.commit()
        first = run_ingest_pipeline(db, document.id)
        assert first.status == "processed"
        second = run_ingest_pipeline(db, document.id)
        assert second.status == "processed"
        assert db.query(SourceSegment).filter_by(document_id=document.id).count() == len(
            second.segments
        )


@pytest.mark.parametrize("filename,mime,maker", [("rule.docx", DOCX_MIME, make_docx)])
def test_upload_docx_processed(client, filename, mime, maker):
    response = upload(client, filename, maker(), mime)
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "processed"
    paths = [segment["heading_path"] for segment in document["segments"]]
    assert ["授权审核", "授权链路"] in paths


def test_upload_image_processed(client):
    response = upload(client, "seal.png", PNG_BYTES, "image/png")
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "processed"
    assert document["segments"][0]["segment_type"] == "image"
    assert "[图片内容 mock 描述]" in document["segments"][0]["content"]
