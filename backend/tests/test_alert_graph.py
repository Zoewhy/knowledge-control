import os
import subprocess
import sys
from pathlib import Path

from app.database import SessionLocal
from app.models import (
    AlertEvent,
    IncidentRecord,
    KnowledgeEntity,
    KnowledgeRelation,
    WikiPage,
    WikiSection,
    WikiSectionEntity,
)
from app.services import graph
from app.services.alert_tools import incident_hypothesis_generate, incident_record_create
from app.services.graph import (
    SqlGraphStore,
    extract_dependencies,
    extract_dependencies_to_graph,
)
from sqlalchemy import func, select


def teardown_function():
    graph.reset_graph_store()


def _relation_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(KnowledgeRelation)) or 0


def _make_alert(service: str = "crm-service", title: str = "CRM 线索搜索不可见") -> AlertEvent:
    with SessionLocal() as db:
        alert = AlertEvent(
            fingerprint=f"fp-{title}-{service}",
            source="test",
            title=title,
            service=service,
            severity="P2",
            description="CRM 搜索不到线索，疑似 MySQL 到 Elasticsearch 同步延迟。",
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)
        return alert


def test_dependency_chain_direction_and_confidence():
    store = SqlGraphStore()
    with SessionLocal() as db:
        assert store.add_dependency(db, "CRM", "MySQL")
        assert store.add_dependency(db, "MySQL", "Elasticsearch")
        assert store.add_dependency(db, "DCheck", "CRM", confidence=0.75)
        db.commit()
        paths = store.dependency_chain(db, "CRM")
        assert ["CRM", "MySQL", "Elasticsearch"] in [item["path"] for item in paths]
        assert ["CRM", "MySQL"] in [item["path"] for item in paths]
        # 方向性：从末端出发没有出边
        assert store.dependency_chain(db, "Elasticsearch") == []
        # 低置信边（0.75 < 0.8）不出现在结果
        assert store.dependency_chain(db, "DCheck") == []
        # 服务名归一化：crm-service 命中 CRM 实体
        assert store.dependency_chain(db, "crm-service")


def test_add_dependency_rejects_non_dictionary_terms():
    store = SqlGraphStore()
    before = _relation_count()
    with SessionLocal() as db:
        assert store.add_dependency(db, "知识库", "Wiki") is False
        assert store.add_dependency(db, "CRM", "知识库") is False
        db.commit()
    assert _relation_count() == before


def test_extract_dependencies_mock_regex_fallback():
    # mock 模式：llm.structured 返回 fallback，走正则抽取，零网络
    text = "CRM 依赖 MySQL 存储线索，MySQL 同步到 Elasticsearch 提供搜索。"
    dependencies = extract_dependencies(text)
    pairs = {(item["subject"], item["object"]) for item in dependencies}
    assert ("CRM", "MySQL") in pairs
    assert ("MySQL", "Elasticsearch") in pairs
    assert all(item["confidence"] == 0.75 for item in dependencies)


def test_extract_dependencies_to_graph_idempotent():
    statement = "CRM 依赖 MySQL 存储线索。"
    with SessionLocal() as db:
        first = extract_dependencies_to_graph(db, statement, claim_id="claim-1")
        db.commit()
        assert first == 1
        count_after_first = _relation_count()
        second = extract_dependencies_to_graph(db, statement, claim_id="claim-1")
        db.commit()
        assert second == 0
        assert _relation_count() == count_after_first


def test_alert_triage_contains_graph_fields():
    store = SqlGraphStore()
    with SessionLocal() as db:
        store.add_dependency(db, "CRM", "MySQL")
        db.commit()
    alert = _make_alert()
    with SessionLocal() as db:
        result = incident_hypothesis_generate(db, {"alert_id": alert.id})
    triage = result["triage"]
    assert "dependency_chain" in triage
    assert "similar_incidents" in triage
    assert ["CRM", "MySQL"] in [item["path"] for item in triage["dependency_chain"]]
    assert triage["similar_incidents"] == []


def test_incident_registration_and_similar_query():
    alert = _make_alert()
    with SessionLocal() as db:
        first = IncidentRecord(
            alert_id=alert.id, title="CRM 线索搜索不可见", severity="P2",
            summary="MySQL 到 Elasticsearch 同步延迟导致搜索缺失。",
        )
        second = IncidentRecord(
            alert_id=alert.id, title="CRM 搜索线索缺失复盘", severity="P2",
            summary="MySQL 到 Elasticsearch 同步延迟导致搜索缺失。",
        )
        db.add_all([first, second])
        db.commit()
        db.refresh(first)
        db.refresh(second)
        store = SqlGraphStore()
        linked = store.register_incident(db, second.id, second.title, [(first.id, 0.62)])
        db.commit()
        assert linked == 1
        neighbors = store.similar_incidents(db, second.title)
        assert neighbors == [{"incident": first.title, "score": 0.62}]
        # 低于 0.5 的相似度不建边
        assert store.register_incident(db, second.id, second.title, [(first.id, 0.3)]) == 0


def test_mark_incident_resolved():
    store = SqlGraphStore()
    with SessionLocal() as db:
        page = WikiPage(slug="crm-es", title="CRM ES 同步手册", summary="s", owner="qa")
        db.add(page)
        db.flush()
        section = WikiSection(page_id=page.id, heading="同步链路", content="c", ordinal=0)
        db.add(section)
        db.flush()
        entity = KnowledgeEntity(name="Elasticsearch", normalized_name="elasticsearch")
        db.add(entity)
        db.flush()
        db.add(WikiSectionEntity(section_id=section.id, entity_id=entity.id))
        db.commit()
        linked = store.mark_incident_resolved(db, "CRM 线索搜索不可见", [section.id])
        db.commit()
        assert linked == 1
        again = store.mark_incident_resolved(db, "CRM 线索搜索不可见", [section.id])
        db.commit()
        assert again == 0
        relation = db.scalar(
            select(KnowledgeRelation).where(KnowledgeRelation.predicate == "resolved_by")
        )
        assert relation is not None
        assert relation.confidence == 1.0


def test_incident_record_create_registers_graph():
    alert = _make_alert()
    with SessionLocal() as db:
        incident_hypothesis_generate(db, {"alert_id": alert.id})
        result = incident_record_create(db, {"alert_id": alert.id, "task_id": None})
        assert result["reused"] is False
        entity = db.scalar(
            select(KnowledgeEntity).where(KnowledgeEntity.entity_type == "incident")
        )
        assert entity is not None
        assert entity.name == alert.title


def test_migration_0006_roundtrip(tmp_path):
    db_path = tmp_path / "migration_check.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"}
    root = Path(__file__).resolve().parents[2]

    def alembic(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root, env=env, capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr

    def has_confidence_column() -> bool:
        import sqlite3

        connection = sqlite3.connect(db_path)
        columns = [row[1] for row in connection.execute("PRAGMA table_info(knowledge_relations)")]
        connection.close()
        return "confidence" in columns

    alembic("upgrade", "head")
    assert has_confidence_column()
    alembic("downgrade", "0005")
    assert not has_confidence_column()
    alembic("upgrade", "head")
    assert has_confidence_column()
