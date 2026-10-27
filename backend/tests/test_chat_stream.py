import io


def _seed_document(client, title: str, content: str) -> None:
    response = client.post("/api/v1/documents", json={"title": title, "content": content})
    assert response.status_code == 201, response.text


def _post_stream(client, payload: dict):
    with client.stream("POST", "/api/v1/chat/stream", json=payload) as response:
        body = "".join(response.iter_text())
    return response, body


def test_chat_stream_emits_sources_tokens_done(client):
    _seed_document(
        client,
        "授权链路审核规则",
        "# 授权链路\n多级授权必须逐级收窄，下游范围不得超过上游授权范围。",
    )
    response, body = _post_stream(
        client, {"query": "授权链路审核要注意什么？", "top_k": 5}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: sources\n" in body
    assert "event: token\n" in body
    assert "event: done\n" in body
    assert body.count("event: token\n") > 1
    assert '"latency_ms"' in body
    # 事件顺序：sources 在最前，done 在最后
    assert body.index("event: sources") < body.index("event: token") < body.rindex("event: done")
    # SSE data 单行 JSON 且 \n\n 结尾
    for chunk in body.split("\n\n"):
        if chunk:
            assert "\ndata: {" in chunk
    # mock 模式答案复用罐头逻辑，标注 [1]
    assert "[1]" in body


def test_chat_stream_empty_knowledge_base(client):
    response, body = _post_stream(client, {"query": "完全不存在的知识点"})
    assert response.status_code == 200
    assert "event: sources\n" in body
    assert "event: token\n" in body
    assert "event: done\n" in body
    assert "知识库中暂未找到足够信息" in body


def test_chat_stream_with_history(client):
    _seed_document(client, "幂等规则", "# 幂等\n发券必须使用稳定业务幂等键。")
    response, body = _post_stream(
        client,
        {
            "query": "再补充一点",
            "history": [
                {"role": "user", "content": "发券为什么要幂等？"},
                {"role": "assistant", "content": "因为重试不能重复发券。"},
                {"role": "system", "content": "ignored role still safe"},
            ],
        },
    )
    assert response.status_code == 200
    assert "event: done\n" in body


def test_chat_stream_search_still_works(client):
    _seed_document(client, "规则", "# 范围\n规则内容文本。")
    response = client.post("/api/v1/search", json={"query": "规则", "include_answer": True})
    assert response.status_code == 200
    assert response.json()["answer"]


def test_chat_stream_uploaded_document_flows(client):
    uploaded = client.post(
        "/api/v1/documents/upload",
        files={
            "file": (
                "rule.md",
                io.BytesIO("# 流式规则\n流式问答需要标注来源。".encode()),
                "text/markdown",
            )
        },
        data={"owner": "qa", "compile": "true"},
    )
    assert uploaded.status_code == 201
    response, body = _post_stream(client, {"query": "流式问答"})
    assert response.status_code == 200
    assert "event: done\n" in body
