import re
from collections import deque
from typing import Any, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    IncidentRecord,
    KnowledgeEntity,
    KnowledgeRelation,
    SourceClaim,
    WikiSection,
    WikiSectionEntity,
)
from .llm import llm

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


MIDDLEWARE_TERMS = ("MySQL", "Elasticsearch", "Redis", "MQ", "Kafka")
SERVICE_TERMS = ("CRM", "DCheck", "营销发券", "退入驻工作流", *MIDDLEWARE_TERMS)


def canonical_service(name: str) -> str | None:
    """把自由文本里的服务名归一到词典实体（如 "Elasticsearch 索引"→"Elasticsearch"）。

    包含式匹配，取最长命中词避免 "CRM" 抢走 "CRM 线索" 之外的歧义。
    """
    normalized = normalize_entity(name)
    if not normalized:
        return None
    matches = [
        term
        for term in SERVICE_TERMS
        if normalize_entity(term) in normalized or normalized in normalize_entity(term)
    ]
    if not matches:
        return None
    return max(matches, key=len)


def is_service_term(name: str) -> bool:
    return canonical_service(name) is not None


def service_entity_type(name: str) -> str:
    normalized = normalize_entity(name)
    if any(normalized == normalize_entity(term) for term in MIDDLEWARE_TERMS):
        return "middleware"
    return "service"


def service_name_candidates(service: str) -> list[str]:
    """归一化告警服务名（如 crm-service）到词典实体名候选。"""
    base = normalize_entity(service)
    candidates = [base]
    for suffix in ("-service", "_service", "service", "-svc", "_svc"):
        if base.endswith(suffix) and len(base) > len(suffix):
            candidates.append(base[: -len(suffix)])
    return [item for item in dict.fromkeys(candidates) if item]


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


def _typed_entity(db: Session, name: str, entity_type: str) -> KnowledgeEntity:
    entity = _entity(db, name)
    if entity.entity_type == "concept" and entity_type != "concept":
        entity.entity_type = entity_type
    return entity


def _match_service_entity(db: Session, service: str) -> Optional[KnowledgeEntity]:
    for candidate in service_name_candidates(service):
        entity = db.scalar(
            select(KnowledgeEntity).where(KnowledgeEntity.normalized_name == candidate)
        )
        if entity:
            return entity
    return None


def _relation_exists(
    db: Session,
    subject_id: str,
    object_id: str,
    predicate: str,
    claim_id: Optional[str],
    bidirectional: bool = False,
) -> bool:
    conditions = [KnowledgeRelation.predicate == predicate]
    if bidirectional:
        conditions.append(
            or_(
                and_(
                    KnowledgeRelation.subject_entity_id == subject_id,
                    KnowledgeRelation.object_entity_id == object_id,
                ),
                and_(
                    KnowledgeRelation.subject_entity_id == object_id,
                    KnowledgeRelation.object_entity_id == subject_id,
                ),
            )
        )
    else:
        conditions.extend(
            [
                KnowledgeRelation.subject_entity_id == subject_id,
                KnowledgeRelation.object_entity_id == object_id,
            ]
        )
    if claim_id is None:
        conditions.append(KnowledgeRelation.claim_id.is_(None))
    else:
        conditions.append(KnowledgeRelation.claim_id == claim_id)
    return db.scalar(select(KnowledgeRelation.id).where(*conditions).limit(1)) is not None


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

    def add_dependency(
        self,
        db: Session,
        subject: str,
        object: str,
        predicate: str = "depends_on",
        claim_id: Optional[str] = None,
        confidence: float = 1.0,
        evidence: str = "",
    ) -> bool:
        if not (is_service_term(subject) and is_service_term(object)):
            return False
        subject_entity = _typed_entity(db, subject, service_entity_type(subject))
        object_entity = _typed_entity(db, object, service_entity_type(object))
        if _relation_exists(db, subject_entity.id, object_entity.id, predicate, claim_id):
            return False
        db.add(
            KnowledgeRelation(
                subject_entity_id=subject_entity.id,
                object_entity_id=object_entity.id,
                predicate=predicate,
                claim_id=claim_id,
                confidence=confidence,
            )
        )
        db.flush()
        return True

    def register_incident(
        self,
        db: Session,
        incident_id: str,
        title: str,
        similar: list[tuple[str, float]],
    ) -> int:
        incident_entity = _typed_entity(db, title, "incident")
        linked = 0
        for other_id, score in similar or []:
            if score < 0.5:
                continue
            other = db.get(IncidentRecord, other_id)
            if not other:
                continue
            other_entity = _typed_entity(db, other.title, "incident")
            if other_entity.id == incident_entity.id:
                continue
            if _relation_exists(
                db, incident_entity.id, other_entity.id, "similar_to", None, bidirectional=True
            ):
                continue
            db.add(
                KnowledgeRelation(
                    subject_entity_id=incident_entity.id,
                    object_entity_id=other_entity.id,
                    predicate="similar_to",
                    claim_id=None,
                    confidence=score,
                )
            )
            linked += 1
        db.flush()
        return linked

    def mark_incident_resolved(
        self, db: Session, incident_title: str, section_ids: list[str]
    ) -> int:
        """SQL 侧以 incident 实体 ↔ section 关联实体表达 RESOLVED_BY 边。"""
        incident_entity = _typed_entity(db, incident_title, "incident")
        linked = 0
        for section_id in section_ids:
            entity_id = db.scalar(
                select(WikiSectionEntity.entity_id)
                .where(WikiSectionEntity.section_id == section_id)
                .limit(1)
            )
            if not entity_id:
                continue
            if _relation_exists(db, incident_entity.id, entity_id, "resolved_by", None):
                continue
            db.add(
                KnowledgeRelation(
                    subject_entity_id=incident_entity.id,
                    object_entity_id=entity_id,
                    predicate="resolved_by",
                    claim_id=None,
                    confidence=1.0,
                )
            )
            linked += 1
        db.flush()
        return linked

    def dependency_chain(
        self, db: Session, service: str, max_depth: int = 3, limit: int = 10
    ) -> list[dict]:
        """沿 depends_on 有向边（subject→object）BFS，仅返回 confidence≥0.8 的边。"""
        start = _match_service_entity(db, service)
        if not start:
            return []
        names = {start.id: start.name}
        paths: list[dict] = []
        queue = deque([(start.id, [start.id], [])])
        while queue and len(paths) < limit:
            entity_id, path_ids, claim_ids = queue.popleft()
            edges = list(
                db.scalars(
                    select(KnowledgeRelation).where(
                        KnowledgeRelation.subject_entity_id == entity_id,
                        KnowledgeRelation.predicate == "depends_on",
                        KnowledgeRelation.confidence >= 0.8,
                    )
                ).all()
            )
            for edge in edges:
                if edge.object_entity_id in path_ids:
                    continue
                next_path = [*path_ids, edge.object_entity_id]
                next_claims = [*claim_ids, *([edge.claim_id] if edge.claim_id else [])]
                for node_id in next_path:
                    if node_id not in names:
                        node = db.get(KnowledgeEntity, node_id)
                        names[node_id] = node.name if node else "unknown"
                paths.append(
                    {"path": [names[node_id] for node_id in next_path], "claim_ids": next_claims}
                )
                if len(next_path) - 1 < max_depth:
                    queue.append((edge.object_entity_id, next_path, next_claims))
                if len(paths) >= limit:
                    break
        return paths

    def similar_incidents(self, db: Session, text_or_id: str, limit: int = 5) -> list[dict]:
        entity = db.scalar(
            select(KnowledgeEntity).where(
                KnowledgeEntity.normalized_name == normalize_entity(text_or_id),
                KnowledgeEntity.entity_type == "incident",
            )
        )
        if not entity:
            return []
        edges = db.scalars(
            select(KnowledgeRelation).where(
                KnowledgeRelation.predicate == "similar_to",
                or_(
                    KnowledgeRelation.subject_entity_id == entity.id,
                    KnowledgeRelation.object_entity_id == entity.id,
                ),
            )
        ).all()
        results = []
        for edge in edges:
            other_id = (
                edge.object_entity_id
                if edge.subject_entity_id == entity.id
                else edge.subject_entity_id
            )
            other = db.get(KnowledgeEntity, other_id)
            if other and other.entity_type == "incident":
                results.append({"incident": other.name, "score": edge.confidence})
        return sorted(results, key=lambda item: item["score"], reverse=True)[:limit]


class Neo4jGraphStore:
    """Neo4j 图存储，生产路径。数据模型：

    (:Section {id,title})-[:MENTIONS]->(:Entity {normalized_name,name})
    (:Entity)-[:CO_OCCURS {claim_id}]->(:Entity)
    (:Entity {type:service|middleware})-[:DEPENDS_ON {claim_id,confidence}]->(:Entity)
    (:Entity {type:incident})-[:SIMILAR_TO {score}]-(:Entity {type:incident})
    (:Entity {type:incident})-[:RESOLVED_BY]->(:Section)
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

    def add_dependency(
        self,
        db: Session,
        subject: str,
        object: str,
        predicate: str = "depends_on",
        claim_id: Optional[str] = None,
        confidence: float = 1.0,
        evidence: str = "",
    ) -> bool:
        if not (is_service_term(subject) and is_service_term(object)):
            return False
        self._run(
            "MERGE (a:Entity {normalized_name: $subject}) "
            "ON CREATE SET a.name = $subject_name "
            "SET a.type = $subject_type "
            "MERGE (b:Entity {normalized_name: $object}) "
            "ON CREATE SET b.name = $object_name "
            "SET b.type = $object_type "
            "MERGE (a)-[r:DEPENDS_ON {claim_id: $claim_id}]->(b) "
            "SET r.confidence = $confidence, r.evidence = $evidence",
            subject=normalize_entity(subject),
            subject_name=subject[:300],
            subject_type=service_entity_type(subject),
            object=normalize_entity(object),
            object_name=object[:300],
            object_type=service_entity_type(object),
            claim_id=claim_id or "",
            confidence=confidence,
            evidence=evidence[:300],
        )
        return True

    def register_incident(
        self,
        db: Session,
        incident_id: str,
        title: str,
        similar: list[tuple[str, float]],
    ) -> int:
        normalized = normalize_entity(title)
        self._run(
            "MERGE (i:Entity {normalized_name: $name}) "
            "ON CREATE SET i.name = $title "
            "SET i.type = 'incident'",
            name=normalized,
            title=title[:300],
        )
        linked = 0
        for other_id, score in similar or []:
            if score < 0.5:
                continue
            other = db.get(IncidentRecord, other_id)
            if not other or normalize_entity(other.title) == normalized:
                continue
            self._run(
                "MERGE (o:Entity {normalized_name: $other}) "
                "ON CREATE SET o.name = $other_title "
                "SET o.type = 'incident' "
                "WITH o "
                "MATCH (i:Entity {normalized_name: $name}) "
                "MERGE (i)-[r:SIMILAR_TO]-(o) "
                "SET r.score = $score",
                other=normalize_entity(other.title),
                other_title=other.title[:300],
                name=normalized,
                score=score,
            )
            linked += 1
        return linked

    def mark_incident_resolved(
        self, db: Session, incident_title: str, section_ids: list[str]
    ) -> int:
        """Neo4j 侧直接连 (incidentEntity)-[:RESOLVED_BY]->(Section)。"""
        if not section_ids:
            return 0
        self._run(
            "MERGE (i:Entity {normalized_name: $name}) "
            "ON CREATE SET i.name = $title "
            "SET i.type = 'incident' "
            "WITH i UNWIND $section_ids AS section_id "
            "MERGE (s:Section {id: section_id}) "
            "MERGE (i)-[:RESOLVED_BY]->(s)",
            name=normalize_entity(incident_title),
            title=incident_title[:300],
            section_ids=section_ids,
        )
        return len(section_ids)

    def dependency_chain(
        self, db: Session, service: str, max_depth: int = 3, limit: int = 10
    ) -> list[dict]:
        records = self._run(
            "MATCH (s:Entity) WHERE s.normalized_name IN $candidates "
            f"MATCH path = (s)-[:DEPENDS_ON*1..{int(max_depth)}]->(t:Entity) "
            "WHERE ALL(r IN relationships(path) WHERE r.confidence >= 0.8) "
            "RETURN [n IN nodes(path) | n.name] AS names, "
            "[r IN relationships(path) | r.claim_id] AS claim_ids "
            "LIMIT $limit",
            candidates=service_name_candidates(service),
            limit=limit,
        )
        return [
            {
                "path": list(record["names"]),
                "claim_ids": [item for item in record["claim_ids"] if item],
            }
            for record in records
        ]

    def similar_incidents(self, db: Session, text_or_id: str, limit: int = 5) -> list[dict]:
        records = self._run(
            "MATCH (i:Entity {normalized_name: $name}) "
            "WHERE i.type = 'incident' "
            "MATCH (i)-[r:SIMILAR_TO]-(o:Entity) "
            "WHERE o.type = 'incident' "
            "RETURN o.name AS name, r.score AS score "
            "ORDER BY score DESC LIMIT $limit",
            name=normalize_entity(text_or_id),
            limit=limit,
        )
        return [{"incident": record["name"], "score": record["score"]} for record in records]


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


DEPENDENCY_VERBS = ("依赖", "调用", "写入", "同步到", "访问")


def _service_occurrences(text: str) -> list[tuple[int, str]]:
    occurrences: list[tuple[int, str]] = []
    lowered = text.lower()
    for term in SERVICE_TERMS:
        start = 0
        needle = term.lower()
        while True:
            index = lowered.find(needle, start)
            if index < 0:
                break
            occurrences.append((index, term))
            start = index + len(needle)
    return sorted(occurrences)


def _extract_dependencies_by_pattern(text: str) -> list[dict]:
    """正则 fallback：X(依赖|调用|写入|同步到|访问)Y，X/Y 取动词两侧最近的服务词典词。"""
    occurrences = _service_occurrences(text)
    results: list[dict] = []
    for verb in DEPENDENCY_VERBS:
        start = 0
        while True:
            position = text.find(verb, start)
            if position < 0:
                break
            start = position + len(verb)
            before = [item for item in occurrences if item[0] + len(item[1]) <= position]
            after = [item for item in occurrences if item[0] >= position + len(verb)]
            if not before or not after:
                continue
            subject, target = before[-1][1], after[0][1]
            if normalize_entity(subject) == normalize_entity(target):
                continue
            evidence = text[max(0, position - 20) : position + len(verb) + 20].strip()
            results.append(
                {
                    "subject": subject,
                    "object": target,
                    "relation": "depends_on",
                    "evidence": evidence[:300],
                    "confidence": 0.75,
                }
            )
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for item in results:
        key = (normalize_entity(item["subject"]), normalize_entity(item["object"]))
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def extract_dependencies(text: str) -> list[dict]:
    """从文本抽取服务/中间件依赖边。LLM 路径 confidence=0.9；正则 fallback=0.75。"""
    result = llm.structured(
        "你是稳定性工程专家。从文本中抽取服务或中间件之间的依赖/调用关系。"
        "只返回 JSON：{dependencies:[{subject,object,relation,evidence}]}，"
        "没有依赖关系时返回空数组。",
        f"文本：{text}",
        {"dependencies": None},
    )
    if isinstance(result, list):
        dependencies = result
    else:
        dependencies = result.get("dependencies")
    if dependencies is None:
        return _extract_dependencies_by_pattern(text)
    extracted: list[dict] = []
    for item in dependencies:
        subject = canonical_service(str(item.get("subject", "")).strip())
        target = canonical_service(str(item.get("object", "")).strip())
        if subject and target and subject != target:
            extracted.append(
                {
                    "subject": subject,
                    "object": target,
                    "relation": str(item.get("relation") or "depends_on"),
                    "evidence": str(item.get("evidence") or "")[:300],
                    "confidence": 0.9,
                }
            )
    return extracted


def extract_dependencies_to_graph(
    db: Session, statement: str, claim_id: Optional[str] = None
) -> int:
    """抽取并写入依赖边，返回新建边数（幂等：已存在的边不重复写）。"""
    written = 0
    for dependency in extract_dependencies(statement):
        if graph_store().add_dependency(
            db,
            dependency["subject"],
            dependency["object"],
            claim_id=claim_id,
            confidence=dependency["confidence"],
            evidence=dependency.get("evidence", ""),
        ):
            written += 1
    return written
