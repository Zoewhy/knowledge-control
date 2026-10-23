import argparse
import json

from sqlalchemy import inspect, text

from app.database import SessionLocal
from app.models import KnowledgeRelation, SourceSegment, WikiCitation, WikiSection
from app.schemas import DocumentCreate
from app.services.compiler import compile_claims
from app.services.graph import index_claim_graph
from app.services.ingestion import ingest_document
from app.services.llm import llm
from app.services.search_index import search_index


def _row_dict(row) -> dict:
    return dict(row._mapping)


def migrate_legacy(db) -> dict:
    tables = set(inspect(db.get_bind()).get_table_names())
    if "knowledge_items" not in tables:
        return {"documents": 0, "claims": 0, "status": "legacy tables not found"}
    chunk_table = "chunks" if "chunks" in tables else "knowledge_chunks"
    items = [_row_dict(row) for row in db.execute(text("SELECT * FROM knowledge_items"))]
    documents = 0
    claims = 0
    for item in items:
        item_id = item["id"]
        chunks = []
        if chunk_table in tables:
            columns = {column["name"] for column in inspect(db.get_bind()).get_columns(chunk_table)}
            foreign_key = "knowledge_item_id" if "knowledge_item_id" in columns else "item_id"
            rows = db.execute(
                text(f"SELECT * FROM {chunk_table} WHERE {foreign_key} = :item_id"),
                {"item_id": item_id},
            )
            chunks = [_row_dict(row) for row in rows]
        chunks.sort(key=lambda row: row.get("ordinal", 0))
        content = "\n\n".join(row["content"] for row in chunks) or item.get("content", "")
        if not content.strip():
            continue
        payload = DocumentCreate(
            title=item.get("title") or f"Legacy knowledge {item_id}",
            content=content,
            source_type="legacy_backfill",
            source_uri=item.get("source_uri") or f"legacy://knowledge-item/{item_id}",
            owner=item.get("owner") or "unknown",
            metadata={"legacy_item_id": str(item_id)},
            compile=False,
        )
        document, created = ingest_document(db, payload)
        if not created:
            continue
        documents += 1
        claim_ids = [claim.id for segment in document.segments for claim in segment.claims]
        claims += len(claim_ids)
        if claim_ids:
            compile_claims(db, claim_ids)
    return {"documents": documents, "claims": claims, "status": "migrated"}


def rebuild_indexes(db) -> dict:
    segments = db.query(SourceSegment).all()
    for segment in segments:
        segment.embedding = llm.embed(segment.content)
        search_index.index_segment(
            segment.id,
            segment.document_id,
            segment.document.title,
            segment.heading,
            segment.content,
        )
    sections = db.query(WikiSection).all()
    for section in sections:
        section.embedding = llm.embed(section.content)
        search_index.index_section(
            section.id,
            section.page_id,
            section.page.title,
            section.heading,
            section.content,
        )
    db.commit()
    return {"segments": len(segments), "sections": len(sections)}


def rebuild_graph(db) -> dict:
    citations = db.query(WikiCitation).all()
    for citation in citations:
        index_claim_graph(db, citation.section, citation.claim)
    db.commit()
    return {"citations": len(citations), "relations": db.query(KnowledgeRelation).count()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy knowledge and rebuild indexes")
    parser.add_argument("--rebuild-only", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        migration = {"skipped": True} if args.rebuild_only else migrate_legacy(db)
        indexes = rebuild_indexes(db)
        graph = rebuild_graph(db)
        print(
            json.dumps(
                {"migration": migration, "indexes": indexes, "graph": graph},
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
