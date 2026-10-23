def sample_document():
    return {
        "title": "召回异常排查手册",
        "owner": "qa",
        "source_uri": "test://retrieval",
        "content": (
            "# 召回排查\n召回率下降时应检查索引更新、特征缺失和模型版本。"
            "确认发布导致异常后需要回滚稳定版本。"
        ),
        "compile": True,
    }


def test_ingest_compile_and_search(client):
    created = client.post("/api/v1/documents", json=sample_document())
    assert created.status_code == 201, created.text
    assert created.json()["segments"][0]["claims"]

    wiki = client.get("/api/v1/wiki")
    assert wiki.status_code == 200
    assert len(wiki.json()) >= 1
    assert wiki.json()[0]["sections"][0]["citations"]

    search = client.post(
        "/api/v1/search",
        json={"query": "召回率下降怎么排查", "top_k": 5, "include_answer": True},
    )
    assert search.status_code == 200, search.text
    body = search.json()
    assert body["hits"]
    assert body["answer"]
    assert body["diagnostics"]["dispatcher"] == "knowledge_search"


def test_agent_approval_and_replay(client):
    client.post("/api/v1/documents", json=sample_document())
    response = client.post(
        "/api/v1/agent/tasks",
        json={"instruction": "为召回异常设计测试用例", "auto_run": True},
    )
    assert response.status_code == 201, response.text
    task = response.json()
    assert task["status"] == "waiting_approval"
    assert task["step_index"] == 1

    approved = client.post(f"/api/v1/agent/tasks/{task['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "succeeded"
    assert len(approved.json()["tool_calls"]) == 2

    replay = client.post(f"/api/v1/agent/tasks/{task['id']}/replay")
    assert replay.status_code == 200
    assert replay.json()["status"] == "waiting_approval"


def test_document_deduplication(client):
    first = client.post("/api/v1/documents", json=sample_document())
    second = client.post("/api/v1/documents", json=sample_document())
    assert first.json()["id"] == second.json()["id"]
    dashboard = client.get("/api/v1/dashboard").json()
    assert dashboard["documents"] == 1


def test_merchant_dataset_profiles_are_deterministic():
    from app.scripts.merchant_seed_data import build_documents

    expected = {"small": 44, "standard": 147, "full": 277}
    for scale, count in expected.items():
        documents = build_documents(scale)
        assert len(documents) == count
        assert len({document.title for document in documents}) == count
        assert all("provenance" in document.metadata for document in documents)
        assert sum(not document.metadata["synthetic"] for document in documents) == 3


def test_chinese_bigram_ranking_prefers_business_phrase():
    from app.services.text import lexical_score

    query = "经销商授权链路断裂"
    expected = "经销商多级授权链路缺少中间授权时进入人工复核"
    unrelated = "核心召回排序链路需要覆盖自动化测试"
    assert lexical_score(query, expected) > lexical_score(query, unrelated)
