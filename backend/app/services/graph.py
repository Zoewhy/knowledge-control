import re
from collections import deque
from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    KnowledgeEntity,
    KnowledgeRelation,
    SourceClaim,
    WikiSection,
    WikiSectionEntity,
)

DOMAIN_ENTITIES = (
    "算法质量",
    "知识库",
    "品牌授权",
    "经销商",
    "商家入驻",
    "营业执照",
    "退入驻",
    "状态机",
    "DCheck",
    "CRM",
    "MySQL",
    "Elasticsearch",
    "营销发券",
    "幂等键",
    "邀请码",
    "智能助手",
    "Agent",
    "Claim",
    "Wiki",
    "GraphRAG",
    "自动化用例",
    "线上告警",
    "Bug 归因",
)


def extract_entities(*texts: str) -> list[str]:
    combined = "\n".join(text for text in texts if text)
    found = [entity for entity in DOMAIN_ENTITIES if entity.lower() in combined.lower()]
    for text in texts:
        for value in re.split(r"[\n#：:，,。；;（）()\[\]]", text or ""):
            value = value.strip()
            if 2 <= len(value) <= 60 and not re.search(r"[。！？!?]", value):
                found.append(value)
    return list(dict.fromkeys(found))[:10]


def normalize_entity(name: str) -> str:
    return re.sub(r"\s+", "", name).lower()[:300]


def _entity(db: Session, name: str) -> KnowledgeEntity:
    normalized = normalize_entity(name)
    entity = db.scalar(
        select(KnowledgeEntity).where(KnowledgeEntity.normalized_name == normalized)
    )
    if entity:
        return entity
    entity = KnowledgeEntity(name=name[:300], normalized_name=normalized)
    db.add(entity)
    db.flush()
    return entity


def _section_title(section: WikiSection) -> str:
    return f"{section.page.title} / {section.heading}"


class SqlGraphStore:
    """基于 SQL 共现表的图存储，本地降级路径。"""

    name = "sql"

    def index(self, db: Session, section: WikiSection, claim: SourceClaim, names: list[str]):
        entities_by_id = {}
        for name in names:
            entity = _entity(db, name)
            entities_by_id[entity.id] = entity
        entities = list(entities_by_id.values())
        for entity in entities:
            exists = db.get(WikiSectionEntity, (section.id, entity.id))
            if not exists:
                db.add(WikiSectionEntity(section_id=section.id, entity_id=entity.id))
                db.flush()
        for index, subject in enumerate(entities):
            for object_entity in entities[index + 1 :]:
                exists = db.scalar(
                    select(KnowledgeRelation).where(
                        KnowledgeRelation.subject_entity_id == subject.id,
                        KnowledgeRelation.object_entity_id == object_entity.id,
                        KnowledgeRelation.predicate == "co_occurs_in_claim",
                        KnowledgeRelation.claim_id == claim.id,
                    )
                )
                if not exists:
                    db.add(
                        KnowledgeRelation(
                            subject_entity_id=subject.id,
                            object_entity_id=object_entity.id,
                            predicate="co_occurs_in_claim",
                            claim_id=claim.id,
                        )
                    )

    def expand(
        self, db: Session, seed_section_ids: set[str], max_depth: int, limit: int
    ) -> tuple[set[str], list[dict]]:
        if not seed_section_ids:
            return set(), []
        seed_entities = set(
            db.scalars(
                select(WikiSectionEntity.entity_id).where(
                    WikiSectionEntity.section_id.in_(seed_section_ids)
                )
            ).all()
        )
        if not seed_entities:
            return set(), []
        queue = deque((entity_id, 0, []) for entity_id in seed_entities)
        seen = set(seed_entities)
        expanded: set[str] = set()
        paths: list[dict] = []
        while queue and len(paths) < limit:
            entity_id, depth, path = queue.popleft()
            if depth >= max_depth:
                continue
            relations = list(
                db.scalars(
                    select(KnowledgeRelation).where(
                        or_(
                            KnowledgeRelation.subject_entity_id == entity_id,
                            KnowledgeRelation.object_entity_id == entity_id,
                        )
                    )
                ).all()
            )
            for relation in relations:
                forward = relation.subject_entity_id == entity_id
                next_id = (
                    relation.object_entity_id if forward else relation.subject_entity_id
                )
                step = {
                    "from_entity_id": entity_id,
                    "to_entity_id": next_id,
                    "predicate": relation.predicate,
                    "direction": "outgoing" if forward else "incoming",
                    "claim_id": relation.claim_id,
                }
                next_path = [*path, step]
                section_ids = set(
                    db.scalars(
                        select(WikiSectionEntity.section_id).where(
                            WikiSectionEntity.entity_id == next_id
                        )
                    ).all()
                )
                new_sections = section_ids - seed_section_ids
                expanded.update(new_sections)
                if new_sections:
                    paths.append({"steps": next_path, "section_ids": sorted(new_sections)})
                if next_id not in seen:
                    seen.add(next_id)
                    queue.append((next_id, depth + 1, next_path))
                if len(paths) >= limit:
                    break
        return expanded, paths[:limit]

    def query_paths(self, db: Session, query: str, limit: int) -> list[dict]:
        entities = list(db.scalars(select(KnowledgeEntity)).all())
        normalized_query = normalize_entity(query)
        matched = [
            entity for entity in entities if normalize_entity(entity.name) in normalized_query
        ]
        if not matched:
            return []
        matched_ids = {entity.id for entity in matched}
        relations = list(
            db.scalars(
                select(KnowledgeRelation).where(
                    or_(
                        KnowledgeRelation.subject_entity_id.in_(matched_ids),
                        KnowledgeRelation.object_entity_id.in_(matched_ids),
                    )
                )
            ).all()
        )
        entity_names = {entity.id: entity.name for entity in entities}
        section_titles = {
            section.id: _section_title(section)
            for section in db.scalars(select(WikiSection)).all()
        }
        paths = []
        for relation in relations:
            section_ids = set(
                db.scalars(
                    select(WikiSectionEntity.section_id).where(
                        WikiSectionEntity.entity_id.in_(
                            [relation.subject_entity_id, relation.object_entity_id]
                        )
                    )
                ).all()
            )
            if len(section_ids) < 2:
                continue
            paths.append(
                {
                    "steps": [
                        {
                            "from": entity_names.get(relation.subject_entity_id, "unknown"),
                            "to": entity_names.get(relation.object_entity_id, "unknown"),
                            "predicate": relation.predicate,
                            "claim_id": relation.claim_id,
                        }
                    ],
                    "sections": [
                        {
                            "section_id": section_id,
                            "title": section_titles.get(section_id, "unknown"),
                        }
                        for section_id in sorted(section_ids)
                    ],
                }
            )
            if len(paths) >= limit:
                break
        return paths


class Neo4jGraphStore:
    """Neo4j 图存储，生产路径。数据模型：

    (:Section {id,title})-[:MENTIONS]->(:Entity {normalized_name,name})
    (:Entity)-[:CO_OCCURS {claim_id}]->(:Entity)
    """

    name = "neo4j"

    def __init__(self, driver: Any) -> None:
        self._driver = driver

    @classmethod
    def connect(cls) -> "Neo4jGraphStore":
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        driver.verify_connectivity()
        return cls(driver)

    def _run(self, cypher: str, **params: Any):
        with self._driver.session() as session:
            return list(session.run(cypher, **params))

    def index(
        self, db: Session, section: WikiSection, claim: SourceClaim, names: list[str]
    ) -> None:
        entities = [{"name": name[:300], "normalized": normalize_entity(name)} for name in names]
        pairs = [
            {"a": entities[i]["normalized"], "b": entities[j]["normalized"]}
            for i in range(len(entities))
            for j in range(i + 1, len(entities))
        ]
        self._run(
            "MERGE (s:Section {id: $section_id}) SET s.title = $title "
            "WITH s UNWIND $entities AS item "
            "MERGE (e:Entity {normalized_name: item.normalized}) "
            "ON CREATE SET e.name = item.name "
            "MERGE (s)-[:MENTIONS]->(e)",
            section_id=section.id,
            title=_section_title(section),
            entities=entities,
        )
        if pairs:
            self._run(
                "UNWIND $pairs AS pair "
                "MATCH (a:Entity {normalized_name: pair.a}), "
                "(b:Entity {normalized_name: pair.b}) "
                "MERGE (a)-[r:CO_OCCURS {claim_id: $claim_id}]->(b)",
                pairs=pairs,
                claim_id=claim.id,
            )

    def expand(
        self, db: Session, seed_section_ids: set[str], max_depth: int, limit: int
    ) -> tuple[set[str], list[dict]]:
        if not seed_section_ids:
            return set(), []
        records = self._run(
            "MATCH (s:Section)-[:MENTIONS]->(seed:Entity) WHERE s.id IN $seed_ids "
            "WITH DISTINCT seed "
            f"MATCH path = (seed)-[:CO_OCCURS*1..{max_depth}]-(neighbor:Entity)"
            "<-[:MENTIONS]-(target:Section) "
            "WHERE NOT target.id IN $seed_ids "
            "RETURN path, collect(DISTINCT target.id) AS section_ids "
            "LIMIT $limit",
            seed_ids=sorted(seed_section_ids),
            limit=limit,
        )
        expanded: set[str] = set()
        paths: list[dict] = []
        for record in records:
            section_ids = set(record["section_ids"])
            expanded.update(section_ids)
            steps = [
                {
                    "from_entity_id": relationship.start_node["normalized_name"],
                    "to_entity_id": relationship.end_node["normalized_name"],
                    "predicate": "co_occurs_in_claim",
                    "direction": "outgoing",
                    "claim_id": relationship.get("claim_id"),
                }
                for relationship in record["path"].relationships
            ]
            paths.append({"steps": steps, "section_ids": sorted(section_ids)})
        return expanded, paths

    def query_paths(self, db: Session, query: str, limit: int) -> list[dict]:
        records = self._run(
            "MATCH (e:Entity) WHERE $q CONTAINS e.normalized_name "
            "WITH collect(e) AS matched "
            "UNWIND matched AS m "
            "MATCH (m)-[r:CO_OCCURS]-(other:Entity) "
            "WITH r, startNode(r) AS a, endNode(r) AS b "
            "MATCH (s:Section)-[:MENTIONS]->(x:Entity) WHERE x IN [a, b] "
            "WITH a, b, r, collect(DISTINCT {section_id: s.id, title: s.title}) AS sections "
            "WHERE size(sections) >= 2 "
            "RETURN a.name AS from_name, b.name AS to_name, r.claim_id AS claim_id, sections "
            "LIMIT $limit",
            q=normalize_entity(query),
            limit=limit,
        )
        return [
            {
                "steps": [
                    {
                        "from": record["from_name"],
                        "to": record["to_name"],
                        "predicate": "co_occurs_in_claim",
                        "claim_id": record["claim_id"],
                    }
                ],
                "sections": [dict(item) for item in record["sections"]],
            }
            for record in records
        ]


_store: Optional[Any] = None


def graph_store():
    global _store
    if _store is None:
        if settings.enable_neo4j:
            try:
                _store = Neo4jGraphStore.connect()
            except Exception:
                _store = SqlGraphStore()
        else:
            _store = SqlGraphStore()
    return _store


def reset_graph_store() -> None:
    global _store
    _store = None


def index_claim_graph(db: Session, section: WikiSection, claim: SourceClaim) -> None:
    names = extract_entities(section.page.title, claim.scope, claim.statement)
    graph_store().index(db, section, claim, names)


def expand_graph(
    db: Session, seed_section_ids: set[str], max_depth: int = 2, limit: int = 12
) -> tuple[set[str], list[dict]]:
    return graph_store().expand(db, seed_section_ids, max_depth, limit)


def query_graph(db: Session, query: str, limit: int = 20) -> list[dict]:
    return graph_store().query_paths(db, query, limit)
