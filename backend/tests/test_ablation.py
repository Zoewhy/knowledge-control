import pytest
from app.database import SessionLocal
from app.models import EvaluationCase
from app.services.evaluation import ARM_FLAGS, run_evaluation
from app.services.retrieval import hybrid_search
from sqlalchemy import select


def _ingest_compiled_document(client, title: str, content: str) -> None:
    response = client.post(
        "/api/v1/documents",
        json={"title": title, "content": content, "compile": True},
    )
    assert response.status_code == 201, response.text


def test_disable_wiki_excludes_wiki_hits(client):
    _ingest_compiled_document(
        client, "CRM 同步手册", "# 同步规则\nCRM 与 MySQL 需要保持同步，补偿写入使用版本号。"
    )
    with SessionLocal() as db:
        baseline = hybrid_search(db, "CRM 与 MySQL 同步规则")
        assert any(hit.section_id for hit in baseline.hits)
        assert baseline.diagnostics["ablation"] == {"disable_wiki": False, "disable_graph": False}

        ablated = hybrid_search(db, "CRM 与 MySQL 同步规则", disable_wiki=True)
    assert ablated.hits
    assert all(not hit.section_id for hit in ablated.hits)
    assert ablated.diagnostics["ablation"] == {"disable_wiki": True, "disable_graph": False}
    assert ablated.diagnostics["fallback_used"] is True
    assert "wiki_lexical" not in ablated.diagnostics["branches"]
    assert "wiki_vector" not in ablated.diagnostics["branches"]


def test_disable_graph_skips_graph_expansion(client):
    _ingest_compiled_document(client, "CRM 同步", "# CRM 规则\nCRM 与 MySQL 需要保持同步。")
    _ingest_compiled_document(client, "CRM 搜索", "# 搜索规则\nCRM 与 Elasticsearch 需要核对版本。")
    with SessionLocal() as db:
        baseline = hybrid_search(db, "CRM 同步规则")
        ablated = hybrid_search(db, "CRM 同步规则", disable_graph=True)
    assert baseline.diagnostics["ablation"]["disable_graph"] is False
    assert ablated.diagnostics["graph_paths"] == []
    assert all("graph_expansion" not in hit.channel for hit in ablated.hits)
    assert ablated.diagnostics["ablation"] == {"disable_wiki": False, "disable_graph": True}


def test_arm_flags_mapping_and_invalid_arm():
    assert ARM_FLAGS == {
        "G0": (True, True),
        "G1": (False, True),
        "G2": (True, False),
        "G3": (False, False),
    }
    with SessionLocal() as db, pytest.raises(ValueError, match="unknown evaluation arm"):
        run_evaluation(db, arm="GX")


def test_run_evaluation_groups_metrics_by_question_type():
    with SessionLocal() as db:
        db.add_all(
            [
                EvaluationCase(name="case-a1", query="CRM 同步", question_type="A"),
                EvaluationCase(name="case-a2", query="MySQL 版本", question_type="A"),
                EvaluationCase(name="case-b1", query="流程", question_type="B"),
            ]
        )
        db.commit()
        run = run_evaluation(db, arm="G1")
        metrics = dict(run.metrics)
    assert metrics["arm"] == "G1"
    assert metrics["cases"] == 3
    assert metrics["by_type"]["A"]["count"] == 2
    assert metrics["by_type"]["B"]["count"] == 1
    for group in metrics["by_type"].values():
        assert "pass_rate" in group
        assert "source_hit" in group
        assert "wiki_hit" in group
        assert "keyword_hit" in group


def test_evaluation_run_endpoint_accepts_arm(client):
    response = client.post("/api/v1/evaluations/runs", json={"arm": "G0"})
    assert response.status_code == 200, response.text
    assert response.json()["metrics"]["arm"] == "G0"

    legacy = client.post("/api/v1/evaluations/runs")
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["metrics"]["arm"] == "G3"

    invalid = client.post("/api/v1/evaluations/runs", json={"arm": "GX"})
    assert invalid.status_code == 422


def test_compare_endpoint_returns_arm_matrix(client):
    with SessionLocal() as db:
        db.add(EvaluationCase(name="case-a", query="CRM", question_type="A"))
        db.commit()
        run_evaluation(db, arm="G1")
        run_evaluation(db, arm="G3")
    response = client.get("/api/v1/evaluations/compare")
    assert response.status_code == 200, response.text
    arms = response.json()["arms"]
    assert set(arms) == {"G1", "G3"}
    assert arms["G1"]["run_id"]
    assert arms["G1"]["by_type"]["A"]["count"] == 1
    assert "pass_rate" in arms["G1"]["overall"]
    assert "arm" not in arms["G1"]["overall"]
    assert "by_type" not in arms["G1"]["overall"]


def test_eval_ablation_seed_is_idempotent_and_typed():
    from app.scripts.eval_ablation_seed import ABLATION_CASES, EXISTING_CASE_TYPES, seed

    with SessionLocal() as db:
        db.add(EvaluationCase(name="退入驻卡单", query="退入驻卡在依赖节点时如何排查？"))
        db.commit()
        first = seed(db)
        second = seed(db)
        legacy = db.scalar(select(EvaluationCase).where(EvaluationCase.name == "退入驻卡单"))
        type_counts = {}
        for case in db.scalars(select(EvaluationCase)).all():
            type_counts[case.question_type] = type_counts.get(case.question_type, 0) + 1
    assert first["created"] == len(ABLATION_CASES) == 70
    assert second["created"] == 0
    assert legacy.question_type == EXISTING_CASE_TYPES["退入驻卡单"] == "E"
    assert type_counts["A"] == 15
    assert type_counts["B"] == 15
    assert type_counts["C"] == 10
    assert type_counts["D"] == 10
    assert type_counts["E"] == 11
    assert type_counts["F"] == 10
