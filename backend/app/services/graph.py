import re
from collections import deque

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

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


def index_claim_graph(db: Session, section: WikiSection, claim: SourceClaim) -> None:
    names = extract_entities(section.page.title, claim.scope, claim.statement)
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


def expand_graph(
    db: Session, seed_section_ids: set[str], max_depth: int = 2, limit: int = 12
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


def query_graph(db: Session, query: str, limit: int = 20) -> list[dict]:
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
        section.id: f"{section.page.title} / {section.heading}"
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
                    {"section_id": section_id, "title": section_titles.get(section_id, "unknown")}
                    for section_id in sorted(section_ids)
                ],
            }
        )
        if len(paths) >= limit:
            break
    return paths
