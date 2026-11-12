"""策略配置与调优实验平台 P0 测试：切分策略、策略解析、重处理、隔离、实验闭环。"""

import pytest
from app.database import SessionLocal
from app.models import IngestPolicy, SourceDocument, SourceSegment
from app.services.chunkers import CHUNKERS, get_chunker
from app.services.policies import resolve_policy
from app.services.reprocess import reprocess
from sqlalchemy import select

TEXT_BLOCKS = [
    {"type": "text", "heading_path": ["指南", "第一章"], "content": "第一段内容。第二句。"},
    {"type": "text", "heading_path": ["指南", "第一章"], "content": "同章第二段内容。"},
    {"type": "text", "heading_path": ["指南", "第二章"], "content": "另一章的内容。"},
]
TABLE_BLOCK = {
    "type": "table",
    "heading_path": ["配置"],
    "headers": ["类目", "保证金"],
    "rows": [[f"类目{i}", str(i * 100)] for i in range(45)],
}


# ---------- 切分策略 ----------


def test_heading_chunker_groups_by_heading_path():
    drafts = get_chunker("heading")(TEXT_BLOCKS, {})
    assert drafts
    assert all(draft.heading_path for draft in drafts)


def test_fixed_chunker_flattens_structure():
    drafts = get_chunker("fixed")(TEXT_BLOCKS, {"max_tokens": 512})
    assert len(drafts) == 1
    assert drafts[0].heading_path == []
    assert "第二句" in drafts[0].content and "另一章" in drafts[0].content


def test_recursive_chunker_merges_same_heading_blocks():
    drafts = get_chunker("recursive")(TEXT_BLOCKS, {"max_tokens": 512})
    assert len(drafts) == 2
    assert drafts[0].heading == "第一章"
    assert "同章第二段内容" in drafts[0].content


def test_page_chunker_groups_by_page_marker():
    blocks = [
        {"type": "text", "heading_path": ["第 1 页"], "content": "第一页内容"},
        {"type": "text", "heading_path": ["第 1 页"], "content": "第一页更多内容"},
        {"type": "text", "heading_path": ["第 2 页"], "content": "第二页内容"},
    ]
    drafts = get_chunker("page")(blocks, {})
    assert len(drafts) == 2
    assert drafts[0].heading_path == ["第 1 页"]
    assert "第一页更多内容" in drafts[0].content


def test_table_row_chunker_splits_large_table_with_header():
    drafts = get_chunker("table_row")([TABLE_BLOCK], {"rows_per_segment": 20})
    assert len(drafts) == 3  # 45 行 → 20 + 20 + 5
    for draft in drafts:
        assert draft.segment_type == "table"
        assert "| 类目 | 保证金 |" in draft.content  # 每段注入表头


def test_table_row_chunker_keeps_small_table_intact():
    block = {**TABLE_BLOCK, "rows": TABLE_BLOCK["rows"][:5]}
    drafts = get_chunker("table_row")([block], {})
    assert len(drafts) == 1
    assert "类目4" in drafts[0].content


def test_unknown_chunker_raises():
    assert set(CHUNKERS) == {"heading", "fixed", "recursive", "page", "table_row"}
    with pytest.raises(ValueError):
        get_chunker("not-exists")


# ---------- 策略解析与 CRUD ----------


def test_policies_crud_and_default_protection(client):
    response = client.get("/api/v1/ingest-policies")
    assert response.status_code == 200
    assert [p["name"] for p in response.json()] == ["default"]

    created = client.post(
        "/api/v1/ingest-policies",
        json={"name": "pdf-fixed", "doc_pattern": "pdf", "chunk_strategy": "fixed"},
    )
    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]

    assert client.post(
        "/api/v1/ingest-policies", json={"name": "pdf-fixed"}
    ).status_code == 409
    assert (
        client.post("/api/v1/ingest-policies", json={"name": "x", "chunk_strategy": "bad"}).status_code
        == 422
    )

    patched = client.patch(
        f"/api/v1/ingest-policies/{policy_id}", json={"chunk_strategy": "recursive"}
    )
    assert patched.json()["chunk_strategy"] == "recursive"
    assert patched.json()["version"] == 2

    default_id = client.get("/api/v1/ingest-policies").json()[0]["id"]
    assert client.patch(f"/api/v1/ingest-policies/{default_id}", json={}).status_code == 409
    assert client.delete(f"/api/v1/ingest-policies/{default_id}").status_code == 409
    assert client.delete(f"/api/v1/ingest-policies/{policy_id}").status_code == 204


def test_resolve_policy_precedence(client):
    client.post(
        "/api/v1/ingest-policies",
        json={"name": "manual-fixed", "doc_pattern": "source_type:manual",
              "chunk_strategy": "fixed"},
    )
    client.post(
        "/api/v1/documents",
        json={"title": "普通文档", "content": "# 标题\n内容段落。", "compile": False},
    )
    with SessionLocal() as db:
        document = db.scalar(select(SourceDocument).where(SourceDocument.title == "普通文档"))
        policy = resolve_policy(db, document)
        assert policy.name == "manual-fixed"  # source_type 命中优先于 default

        explicit = db.scalar(select(IngestPolicy).where(IngestPolicy.name == "manual-fixed"))
        document.metadata_json = {"policy_id": explicit.id}
        assert resolve_policy(db, document).id == explicit.id  # 文档指定最高优先

        document.metadata_json = {}
        document.source_type = "other"
        assert resolve_policy(db, document).name == "default"  # 兜底


# ---------- 重处理与实验隔离 ----------


def _upload_txt(client, title: str, content: str) -> str:
    response = client.post(
        "/api/v1/documents/upload",
        files={"file": ("notes.txt", content.encode("utf-8"), "text/plain")},
        data={"title": title, "compile": "false"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "processed"
    return response.json()["id"]


def _policy_snapshot(**overrides) -> dict:
    snapshot = {
        "name": "exp",
        "parser_strategy": "auto",
        "chunk_strategy": "heading",
        "chunk_params": {},
        "min_text_chars": 100,
        "auto_compile": False,
    }
    snapshot.update(overrides)
    return snapshot


def test_reprocess_copies_tagged_idempotent_and_cached(client, monkeypatch):
    document_id = _upload_txt(client, "重处理原文", "# 指南\n第一段规则内容。第二段规则内容。")

    with SessionLocal() as db:
        stats1 = reprocess(db, [document_id], _policy_snapshot(), "run-1")
        assert stats1["docs"] == 1 and stats1["segments"] > 0
        copies = db.scalars(
            select(SourceDocument).where(SourceDocument.title.like "[实验]%")
        ).all()
        assert len(copies) == 1
        assert copies[0].metadata_json["experiment_run_id"] == "run-1"

        # 幂等：同 run_id 重跑，先删旧副本再重建，数量不变
        stats_again = reprocess(db, [document_id], _policy_snapshot(), "run-1")
        assert stats_again["docs"] == 1
        assert db.scalars(
            select(SourceDocument).where(SourceDocument.title.like "[实验]%")
        ).all()
        count = len(
            db.scalars(select(SourceDocument).where(SourceDocument.title.like "[实验]%")).all()
        )
        assert count == 1

        # claim 缓存：内容相同的第二次重处理不应再调 LLM 抽取
        from app.services import reprocess as reprocess_module

        calls = 0
        real_extract = reprocess_module.llm.extract_claims

        def counting_extract(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_extract(*args, **kwargs)

        monkeypatch.setattr(reprocess_module.llm, "extract_claims", counting_extract)
        stats2 = reprocess(db, [document_id], _policy_snapshot(), "run-2")
        assert stats2["llm_calls"] == 0
        assert stats2["cache_hits"] == stats2["segments"]


def test_main_retrieval_excludes_experiment_copies(client):
    document_id = _upload_txt(client, "隔离原文", "# 隔离\n独有的斑马词条知识内容。")
    with SessionLocal() as db:
        reprocess(db, [document_id], _policy_snapshot(), "run-iso")
        copy_segment_ids = set(
            db.scalars(
                select(SourceSegment.id)
                .join(SourceDocument, SourceSegment.document_id == SourceDocument.id)
                .where(SourceDocument.metadata_json["experiment_run_id"].as_string() == "run-iso")
            ).all()
        )
        assert copy_segment_ids
        from app.services.retrieval import hybrid_search

        result = hybrid_search(db, "独有的斑马词条", include_answer=False)
        hit_segment_ids = {hit.segment_id for hit in result.hits if hit.segment_id}
        assert hit_segment_ids
        assert not (hit_segment_ids & copy_segment_ids)  # 默认排除实验副本


# ---------- 问答集导入 ----------


def test_evaluation_set_import_idempotent_and_warns(client):
    _upload_txt(client, "导入源文档", "# 说明\n知识内容。")
    payload = {
        "eval_group": "exp-group",
        "cases": [
            {"query": "知识内容是什么", "expected_sources": ["导入源文档标题不存在"], "question_type": "A"},
        ],
    }
    first = client.post("/api/v1/evaluations/sets/import", json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["created"] == 1
    assert first.json()["warnings"]  # 期望来源不存在 → warning 不拦截

    second = client.post("/api/v1/evaluations/sets/import", json=payload)
    assert second.json()["created"] == 0
    assert second.json()["updated"] == 1  # 幂等


# ---------- 策略对比实验端到端 ----------


def test_experiment_end_to_end_matrix(client):
    document_id = _upload_txt(client, "实验源文档", "# 保证金\n服饰类目保证金为五千元规则说明。")
    client.post(
        "/api/v1/evaluations/sets/import",
        json={
            "eval_group": "exp-e2e",
            "cases": [{"query": "服饰类目保证金是多少", "expected_sources": [], "question_type": "A"}],
        },
    )
    heading = client.post("/api/v1/ingest-policies", json={"name": "e2e-heading"}).json()
    fixed = client.post(
        "/api/v1/ingest-policies", json={"name": "e2e-fixed", "chunk_strategy": "fixed"}
    ).json()

    created = client.post(
        "/api/v1/experiments",
        json={
            "name": "切分对比",
            "document_ids": [document_id],
            "policy_ids": [heading["id"], fixed["id"]],
            "eval_group": "exp-e2e",
        },
    )
    assert created.status_code == 201, created.text
    experiment = created.json()
    assert len(experiment["runs"]) == 2

    detail = client.get(f"/api/v1/experiments/{experiment['id']}").json()
    for run in detail["runs"]:
        assert run["status"] == "done", run
        assert run["metrics"]["pass_rate"] is not None
        assert "by_type" in run["metrics"]
        assert run["policy_snapshot"]["name"] in {"e2e-heading", "e2e-fixed"}

    done_run = next(run for run in detail["runs"] if run["status"] == "done")
    applied = client.post(
        f"/api/v1/experiments/{experiment['id']}/apply", json={"run_id": done_run["id"]}
    )
    assert applied.status_code == 200
    assert applied.json()["applied_run_id"] == done_run["id"]
