from types import SimpleNamespace

from app.config import settings
from app.services import graph


def teardown_function():
    graph.reset_graph_store()


class FakeSession:
    def __init__(self, driver):
        self.driver = driver

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, cypher, **params):
        self.driver.calls.append((cypher, params))
        return self.driver.responses.pop(0) if self.driver.responses else []


class FakeDriver:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def session(self):
        return FakeSession(self)


class FakeRelationship:
    def __init__(self, source, target, claim_id):
        self.start_node = {"normalized_name": source}
        self.end_node = {"normalized_name": target}
        self._claim_id = claim_id

    def get(self, key):
        return self._claim_id if key == "claim_id" else None


def section_stub():
    return SimpleNamespace(
        id="sec-1",
        page=SimpleNamespace(title="MySQL 与 Elasticsearch"),
        heading="同步链路",
    )


def claim_stub():
    return SimpleNamespace(id="claim-1")


def test_sql_store_is_default():
    assert settings.enable_neo4j is False
    store = graph.graph_store()
    assert store.name == "sql"


def test_neo4j_connect_failure_falls_back_to_sql(monkeypatch):
    monkeypatch.setattr(settings, "enable_neo4j", True)

    def broken_connect():
        raise ConnectionError("neo4j unreachable")

    monkeypatch.setattr(graph.Neo4jGraphStore, "connect", staticmethod(broken_connect))
    store = graph.graph_store()
    assert store.name == "sql"


def test_neo4j_store_selected_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "enable_neo4j", True)
    monkeypatch.setattr(
        graph.Neo4jGraphStore, "connect", staticmethod(lambda: graph.Neo4jGraphStore(FakeDriver()))
    )
    store = graph.graph_store()
    assert store.name == "neo4j"


def test_neo4j_index_writes_sections_entities_and_pairs():
    driver = FakeDriver()
    store = graph.Neo4jGraphStore(driver)
    store.index(None, section_stub(), claim_stub(), ["MySQL", "Elasticsearch", "DCheck"])
    assert len(driver.calls) == 2
    section_cypher, section_params = driver.calls[0]
    assert "MERGE (s:Section" in section_cypher
    assert "MERGE (e:Entity" in section_cypher
    assert section_params["section_id"] == "sec-1"
    assert section_params["title"] == "MySQL 与 Elasticsearch / 同步链路"
    normalized = {item["normalized"] for item in section_params["entities"]}
    assert normalized == {"mysql", "elasticsearch", "dcheck"}
    pair_cypher, pair_params = driver.calls[1]
    assert "MERGE (a)-[r:CO_OCCURS {claim_id: $claim_id}]->(b)" in pair_cypher
    assert pair_params["claim_id"] == "claim-1"
    assert len(pair_params["pairs"]) == 3


def test_neo4j_query_paths_maps_records():
    driver = FakeDriver(
        responses=[
            [
                {
                    "from_name": "MySQL",
                    "to_name": "Elasticsearch",
                    "claim_id": "claim-1",
                    "sections": [
                        {"section_id": "sec-1", "title": "同步链路"},
                        {"section_id": "sec-2", "title": "延迟排查"},
                    ],
                }
            ]
        ]
    )
    store = graph.Neo4jGraphStore(driver)
    paths = store.query_paths(None, "MySQL 与 Elasticsearch 关系", limit=10)
    cypher, params = driver.calls[0]
    assert "CONTAINS e.normalized_name" in cypher
    assert params["q"] == "mysql与elasticsearch关系"
    assert paths[0]["steps"][0]["from"] == "MySQL"
    assert paths[0]["steps"][0]["claim_id"] == "claim-1"
    assert len(paths[0]["sections"]) == 2


def test_neo4j_expand_maps_variable_length_paths():
    relationship = FakeRelationship("mysql", "elasticsearch", "claim-1")
    driver = FakeDriver(
        responses=[
            [{"path": SimpleNamespace(relationships=[relationship]), "section_ids": ["sec-9"]}]
        ]
    )
    store = graph.Neo4jGraphStore(driver)
    expanded, paths = store.expand(None, {"sec-1"}, max_depth=2, limit=12)
    cypher, params = driver.calls[0]
    assert "CO_OCCURS*1..2" in cypher
    assert params["seed_ids"] == ["sec-1"]
    assert expanded == {"sec-9"}
    assert paths[0]["steps"][0]["claim_id"] == "claim-1"


def test_neo4j_expand_short_circuits_without_seeds():
    store = graph.Neo4jGraphStore(FakeDriver())
    assert store.expand(None, set(), 2, 12) == (set(), [])
