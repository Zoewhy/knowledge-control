import io

from app.database import SessionLocal
from app.models import EvaluationCase, SourceClaim, WikiPage
from app.schemas import CitationRead, SearchHit, SearchResponse
from app.services.evaluation import citation_validity
from app.services.graph import query_graph
from app.worker import execute_agent_task


def test_upload_batch_and_feishu_ingestion(client, monkeypatch):
    uploaded = client.post(
        "/api/v1/documents/upload",
        files={
            "file": (
                "rule.md",
                io.BytesIO("# 上传规则\n上传文档需要保留来源和解析证据。".encode()),
                "text/markdown",
            )
        },
        data={"owner": "qa", "compile": "true"},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["source_type"] == "file_upload"

    batch = client.post(
        "/api/v1/documents/batch",
        json={
            "documents": [
                {"title": "批量一", "content": "# 批量规则一\n批量导入必须保留原始来源。"},
                {"title": "批量二", "content": "# 批量规则二\n批量导入必须支持幂等处理。"},
            ]
        },
    )
    assert batch.status_code == 201
    assert batch.json()["total"] == 2

    monkeypatch.setattr(
        "app.api.feishu_client.read_document", lambda token: "# 飞书规则\n飞书文档需要同步原文。"
    )
    feishu = client.post(
        "/api/v1/documents/feishu", json={"document_token": "doc-token", "compile": True}
    )
    assert feishu.status_code == 201, feishu.text
    assert feishu.json()["source_uri"] == "feishu://docx/doc-token"


def test_review_supports_scope_owner_and_content_changes(client):
    first = client.post(
        "/api/v1/documents",
        json={"title": "审核规则", "content": "# 审核范围\n原始规则需要人工复核。"},
    )
    section_id = first.json()["segments"][0]["id"]
    second = client.post(
        "/api/v1/documents",
        json={"title": "审核补充", "content": "# 审核范围\n新增规则存在不同的适用条件。"},
    )
    assert second.status_code == 201
    reviews = client.get("/api/v1/reviews", params={"status": "pending"}).json()
    assert reviews
    review = reviews[0]
    assert review["source_context"]["segment_content"]
    wiki = client.get("/api/v1/wiki").json()
    target_section_id = wiki[0]["sections"][0]["id"]
    decided = client.post(
        f"/api/v1/reviews/{review['id']}/decision",
        json={
            "decision": "approve",
            "reviewer": "qa",
            "scope": "调整后的 Scope",
            "owner": "算法质量",
            "target_section_id": target_section_id,
            "proposed_content": "审核后的确定性内容。",
        },
    )
    assert decided.status_code == 200, decided.text
    assert client.get("/api/v1/wiki").json()[0]["owner"] == "算法质量"
    with SessionLocal() as db:
        assert db.get(SourceClaim, review["claim_id"]).scope == "调整后的 Scope"
        assert "审核后的确定性内容" in db.get(WikiPage, wiki[0]["id"]).sections[0].content
    assert section_id != target_section_id


def test_source_fallback_has_real_citations(client):
    client.post(
        "/api/v1/documents",
        json={
            "title": "未编译排查记录",
            "content": "# 专用排查\nXenoScope 专用故障需要检查消息游标。",
            "compile": False,
        },
    )
    response = client.post("/api/v1/search", json={"query": "XenoScope 怎么排查"})
    assert response.status_code == 200
    body = response.json()
    assert body["diagnostics"]["fallback_used"] is True
    assert body["hits"][0]["citations"][0]["document_title"] == "未编译排查记录"
    assert body["diagnostics"]["branches"]["source_lexical"]["hits"] >= 1


def test_dispatcher_and_branch_degradation(client, monkeypatch):
    direct = client.post("/api/v1/search", json={"query": "你好"})
    assert direct.json()["diagnostics"]["dispatcher"] == "direct_answer"
    assert direct.json()["hits"] == []

    def fail_vector(*args):
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr("app.services.retrieval._wiki_vector", fail_vector)
    response = client.post("/api/v1/search", json={"query": "任意知识"})
    assert response.status_code == 200
    assert response.json()["diagnostics"]["branches"]["wiki_vector"]["degraded"] is True


def test_graph_paths_use_persisted_relations(client):
    client.post(
        "/api/v1/documents",
        json={"title": "CRM 同步", "content": "# CRM 规则\nCRM 与 MySQL 需要保持同步。"},
    )
    client.post(
        "/api/v1/documents",
        json={"title": "CRM 搜索", "content": "# 搜索规则\nCRM 与 Elasticsearch 需要核对版本。"},
    )
    pending = client.get("/api/v1/reviews", params={"status": "pending"}).json()
    if pending:
        approved = client.post(
            f"/api/v1/reviews/{pending[0]['id']}/decision",
            json={"decision": "approve", "reviewer": "qa"},
        )
        assert approved.status_code == 200, approved.text
    with SessionLocal() as db:
        paths = query_graph(db, "CRM 关系")
    assert paths
    assert paths[0]["steps"][0]["claim_id"]
    assert paths[0]["sections"]


def test_celery_dispatch_is_real_queue_call(client, monkeypatch):
    monkeypatch.setattr("app.config.settings.agent_execution_mode", "celery")
    calls = []
    monkeypatch.setattr(execute_agent_task, "delay", lambda *args: calls.append(args))
    task = client.post(
        "/api/v1/agent/tasks", json={"instruction": "查询知识库规则", "auto_run": False}
    ).json()
    resumed = client.post(f"/api/v1/agent/tasks/{task['id']}/resume")
    assert resumed.status_code == 200
    assert calls == [(task["id"], False)]


def test_agent_artifact_reflow_and_events(client):
    client.post(
        "/api/v1/documents",
        json={"title": "用例知识", "content": "# 用例设计\n测试用例需要包含异常路径。"},
    )
    task = client.post(
        "/api/v1/agent/tasks", json={"instruction": "为权限校验设计测试用例"}
    ).json()
    approved = client.post(f"/api/v1/agent/tasks/{task['id']}/approve")
    assert approved.status_code == 200
    body = approved.json()
    assert body["artifacts"]
    assert {event["event_type"] for event in body["events"]} >= {
        "task_created",
        "task_started",
        "approval_required",
        "task_succeeded",
    }
    artifact = body["artifacts"][0]
    reflowed = client.post(f"/api/v1/agent/artifacts/{artifact['id']}/reflow")
    assert reflowed.status_code == 200, reflowed.text
    assert reflowed.json()["source_type"] == "agent_artifact"
    repeated = client.post(f"/api/v1/agent/artifacts/{artifact['id']}/reflow")
    assert repeated.json()["id"] == reflowed.json()["id"]


def test_citation_validity_checks_answer_references_and_evidence():
    case = EvaluationCase(name="citation", query="q")
    citation = CitationRead(
        claim_id="claim",
        statement="statement",
        document_id="doc",
        document_title="doc",
        segment_id="segment",
    )
    hit = SearchHit(
        channel="wiki",
        score=1,
        title="wiki",
        content="content",
        source=citation,
        citations=[citation],
    )
    valid = SearchResponse(query="q", answer="答案 [1]", hits=[hit], diagnostics={}, latency_ms=1)
    invalid = valid.model_copy(update={"answer": "答案 [2]"})
    assert citation_validity(case, valid) == 1
    assert citation_validity(case, invalid) == 0


def test_benchmark_has_25_cases():
    from app.scripts.merchant_seed_data import EVALUATION_CASES

    assert len(EVALUATION_CASES) == 25


def test_failed_tool_records_structured_diagnostics():
    from app.models import AgentTask
    from app.services.agent_runtime import ToolDefinition, execute_task, registry

    def broken_handler(db, args):
        raise RuntimeError("tool exploded")

    registry.register(ToolDefinition("broken_tool", "broken", "low", broken_handler))
    try:
        with SessionLocal() as db:
            task = AgentTask(
                instruction="触发失败", plan=[{"tool": "broken_tool", "arguments": {}}]
            )
            db.add(task)
            db.commit()
            execute_task(db, task)
            assert task.status.value == "failed"
            assert task.error_detail["tool"] == "broken_tool"
            assert task.tool_calls[0].status == "failed"
            assert task.events[-1].event_type == "task_failed"
    finally:
        registry._tools.pop("broken_tool", None)
