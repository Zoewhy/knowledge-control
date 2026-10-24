import json

from app.config import settings
from app.services.feishu import FeishuError, feishu_client


def feishu_message_event(text, event_id="evt-1", message_id="m-1", chat_id="chat-1", token=""):
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "token": token,
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou-1"}},
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def sample_doc():
    return {
        "title": "召回异常排查手册",
        "owner": "qa",
        "content": "# 召回排查\n召回率下降时应检查索引更新、特征缺失和模型版本。",
        "compile": True,
    }


def test_feishu_url_verification(client, monkeypatch):
    monkeypatch.setattr(settings, "feishu_verification_token", "tok")
    ok = client.post(
        "/api/v1/integrations/feishu/events",
        json={"type": "url_verification", "challenge": "c-1", "token": "tok"},
    )
    assert ok.status_code == 200
    assert ok.json() == {"challenge": "c-1"}

    bad = client.post(
        "/api/v1/integrations/feishu/events",
        json={"type": "url_verification", "challenge": "c-1", "token": "wrong"},
    )
    assert bad.status_code == 401


def test_feishu_event_dedupe(client):
    event = feishu_message_event("问答:如何排查召回率下降", event_id="evt-dup")
    first = client.post("/api/v1/integrations/feishu/events", json=event)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "processed"
    second = client.post("/api/v1/integrations/feishu/events", json=event)
    assert second.json()["status"] == "duplicate"

    events = client.get("/api/v1/integrations/events").json()
    assert len(events) == 1
    tasks = client.get("/api/v1/agent/tasks").json()
    assert len(tasks) == 1


def test_feishu_message_routes_to_qa_and_notify_failure(client):
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json=feishu_message_event("问答:如何排查召回率下降", event_id="evt-qa"),
    )
    body = response.json()
    assert body["route"] == "qa"
    task_id = body["agent_task_id"]
    task = client.get("/api/v1/agent/tasks").json()[0]
    assert task["id"] == task_id
    assert [step["tool"] for step in task["plan"]] == ["knowledge_search", "feishu_notify"]
    assert task["status"] == "waiting_approval"

    approved = client.post(f"/api/v1/agent/tasks/{task_id}/approve")
    assert approved.status_code == 200, approved.text
    task = approved.json()
    assert task["status"] == "succeeded"
    notify_call = next(c for c in task["tool_calls"] if c["tool_name"] == "feishu_notify")
    assert notify_call["output"]["sent"] is False
    assert "凭证" in notify_call["output"]["error"]

    event = client.get("/api/v1/integrations/events").json()[0]
    assert event["status"] == "processed"
    assert event["agent_task_id"] == task_id


def test_feishu_notify_success_payload(client, monkeypatch):
    sent = {}

    def fake_send(receive_id, text, receive_id_type="chat_id"):
        sent["receive_id"] = receive_id
        sent["text"] = text
        return {"message_id": "om-1"}

    monkeypatch.setattr(feishu_client, "send_text", fake_send)
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json=feishu_message_event("问答:召回率下降怎么排查", event_id="evt-notify"),
    )
    task_id = response.json()["agent_task_id"]
    client.post(f"/api/v1/agent/tasks/{task_id}/approve")
    assert sent["receive_id"] == "chat-1"
    assert "召回率下降" in sent["text"]


def test_feishu_message_routes_to_qa_generate(client):
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json=feishu_message_event("用例:登录功能 PRD：支持账号密码登录", event_id="evt-case"),
    )
    body = response.json()
    assert body["route"] == "qa_generate"
    assert body["requirement_id"]
    task = client.get("/api/v1/agent/tasks").json()[0]
    tools = [step["tool"] for step in task["plan"]]
    assert tools[:6] == [
        "prd_parse",
        "knowledge_search",
        "testpoint_generate",
        "testcase_generate",
        "coverage_review",
        "report_generate",
    ]
    requirements = client.get("/api/v1/qa/requirements").json()
    assert len(requirements) == 1
    assert "登录" in requirements[0]["title"]


def test_feishu_message_routes_to_alert_triage(client):
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json=feishu_message_event("归因:推荐服务召回率下降", event_id="evt-alert"),
    )
    body = response.json()
    assert body["route"] == "alert_triage"
    alerts = client.get("/api/v1/alerts").json()
    assert len(alerts) == 1
    assert alerts[0]["source"] == "feishu"
    task = client.get("/api/v1/agent/tasks").json()[0]
    tools = [step["tool"] for step in task["plan"]]
    assert tools[:5] == [
        "alert_context_collect",
        "knowledge_search",
        "graph_query",
        "incident_hypothesis_generate",
        "incident_record_create",
    ]


def test_feishu_doc_message_triggers_ingestion(client, monkeypatch):
    monkeypatch.setattr(
        feishu_client, "read_document", lambda token: "# 飞书文档\n索引更新延迟会导致召回下降。"
    )
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json=feishu_message_event("文档 doc-token-1", event_id="evt-doc"),
    )
    body = response.json()
    assert body["status"] == "processed"
    assert body["document_created"] is True
    documents = client.get("/api/v1/documents").json()
    assert documents[0]["source_type"] == "feishu"
    assert documents[0]["segments"][0]["claims"]


def test_feishu_unknown_message_ignored(client):
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json=feishu_message_event("你好", event_id="evt-hi"),
    )
    assert response.json()["status"] == "ignored"
    events = client.get("/api/v1/integrations/events").json()
    assert events[0]["status"] == "ignored"


def test_qa_requirement_full_chain(client):
    client.post("/api/v1/documents", json=sample_doc())
    created = client.post(
        "/api/v1/qa/requirements",
        json={
            "title": "登录功能上线",
            "content": "支持账号密码登录，错误五次锁定，需覆盖登录与权限场景。",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    requirement_id = body["requirement"]["id"]
    task_id = body["agent_task_id"]

    task = client.get("/api/v1/agent/tasks").json()[0]
    assert task["status"] == "waiting_approval"
    assert task["plan"][task["step_index"]]["tool"] == "testcase_generate"

    approved = client.post(f"/api/v1/agent/tasks/{task_id}/approve")
    assert approved.status_code == 200, approved.text
    task = approved.json()
    assert task["status"] == "succeeded"
    assert any(a["artifact_type"] == "markdown" for a in task["artifacts"])

    suites = client.get(f"/api/v1/qa/test-suites?requirement_id={requirement_id}").json()
    assert len(suites) == 1
    suite = suites[0]
    assert suite["cases"], "应生成结构化用例"
    assert suite["summary"]["coverage"] >= 0.5
    assert any("登录" in point["name"] for point in suite["testpoints"])

    requirement = client.get("/api/v1/qa/requirements").json()[0]
    assert requirement["parsed"]["goals"]

    report = client.post(f"/api/v1/qa/test-suites/{suite['id']}/report")
    assert report.status_code == 200
    assert report.json()["markdown"].startswith("# 登录功能上线 测试报告")
    assert report.json()["artifact_id"]

    reflow = client.post(f"/api/v1/qa/test-suites/{suite['id']}/reflow")
    assert reflow.status_code == 200
    assert reflow.json()["source_type"] == "qa_report"


def test_qa_testpoints_vary_by_requirement(client):
    first = client.post(
        "/api/v1/qa/requirements",
        json={"title": "登录功能", "content": "账号密码登录与权限控制"},
    ).json()
    second = client.post(
        "/api/v1/qa/requirements",
        json={"title": "数据导入", "content": "支持批量导入与重复数据幂等"},
    ).json()
    suites_first = client.get(
        f"/api/v1/qa/test-suites?requirement_id={first['requirement']['id']}"
    ).json()
    suites_second = client.get(
        f"/api/v1/qa/test-suites?requirement_id={second['requirement']['id']}"
    ).json()
    names_first = {p["name"] for p in suites_first[0]["testpoints"]}
    names_second = {p["name"] for p in suites_second[0]["testpoints"]}
    assert names_first != names_second
    assert any("登录" in name for name in names_first)
    assert any("导入" in name for name in names_second)


def test_alert_webhook_dedup_and_triage_flow(client):
    client.post("/api/v1/documents", json=sample_doc())
    payload = {
        "source": "prometheus",
        "title": "推荐服务召回率下降",
        "service": "recommendation",
        "severity": "P1",
        "description": "召回率五分钟下跌 30%",
        "labels": {"cluster": "pre1"},
    }
    first = client.post("/api/v1/integrations/alerts", json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["created"] is True
    alert_id = first.json()["alert"]["id"]
    task_id = first.json()["agent_task_id"]

    second = client.post("/api/v1/integrations/alerts", json=payload)
    assert second.json()["created"] is False
    assert second.json()["alert"]["occurrence_count"] == 2

    alerts = client.get("/api/v1/alerts").json()
    assert len(alerts) == 1
    tasks = client.get("/api/v1/agent/tasks").json()
    assert len(tasks) == 1

    # 未审批前：任务停在 waiting_approval，不产生事故记录
    task = tasks[0]
    assert task["status"] == "waiting_approval"
    assert client.get("/api/v1/incidents").json() == []

    approved = client.post(f"/api/v1/agent/tasks/{task_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "succeeded"

    alert = client.get(f"/api/v1/alerts/{alert_id}").json()
    assert alert["triage"]["hypotheses"]
    assert alert["triage"]["uncertainty"]
    assert alert["triage"]["knowledge_references"]

    incidents = client.get("/api/v1/incidents").json()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["alert_id"] == alert_id
    assert incident["root_cause_hypothesis"]
    assert incident["recommended_actions"]
    assert "uncertainty" in incident["references"]

    again = client.post(f"/api/v1/alerts/{alert_id}/triage")
    assert again.json()["reused"] is True
    assert again.json()["agent_task_id"] == task_id


def test_alert_manual_fingerprint_and_triage_endpoint(client):
    payload = {
        "title": "库存服务错误率升高",
        "source": "grafana",
        "fingerprint": "fp-1",
        "auto_triage": False,
    }
    created = client.post("/api/v1/integrations/alerts", json=payload)
    assert created.json()["created"] is True
    assert created.json()["agent_task_id"] is None
    alert_id = created.json()["alert"]["id"]

    triage = client.post(f"/api/v1/alerts/{alert_id}/triage")
    assert triage.status_code == 201
    assert triage.json()["reused"] is False
    assert triage.json()["agent_task_id"]


def test_feishu_notify_failure_does_not_fail_task(client, monkeypatch):
    def broken_send(receive_id, text, receive_id_type="chat_id"):
        raise FeishuError("simulated network failure")

    monkeypatch.setattr(feishu_client, "send_text", broken_send)
    response = client.post(
        "/api/v1/integrations/feishu/events",
        json=feishu_message_event("问答:如何排查召回率下降", event_id="evt-fail"),
    )
    task_id = response.json()["agent_task_id"]
    approved = client.post(f"/api/v1/agent/tasks/{task_id}/approve")
    task = approved.json()
    assert task["status"] == "succeeded"
    notify_call = next(c for c in task["tool_calls"] if c["tool_name"] == "feishu_notify")
    assert notify_call["output"] == {"sent": False, "error": "simulated network failure"}
