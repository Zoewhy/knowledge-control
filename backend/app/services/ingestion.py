from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SourceClaim, SourceDocument, SourceSegment
from ..schemas import DocumentCreate
from .llm import llm
from .search_index import search_index
from .text import fingerprint, split_markdown


def ingest_document(db: Session, payload: DocumentCreate) -> tuple[SourceDocument, bool]:
    checksum = fingerprint(payload.content)
    existing = db.scalar(select(SourceDocument).where(SourceDocument.checksum == checksum))
    if existing:
        return existing, False

    document = SourceDocument(
        title=payload.title,
        source_type=payload.source_type,
        source_uri=payload.source_uri,
        owner=payload.owner,
        checksum=checksum,
        metadata_json=payload.metadata,
    )
    db.add(document)
    db.flush()
    for ordinal, (heading, content) in enumerate(split_markdown(payload.content)):
        segment = SourceSegment(
            document_id=document.id,
            ordinal=ordinal,
            heading=heading,
            content=content,
            token_count=len(content),
            embedding=llm.embed(content),
        )
        db.add(segment)
        db.flush()
        search_index.index_segment(
            segment.id, document.id, document.title, segment.heading, segment.content
        )
        for item in llm.extract_claims(content, heading):
            statement = str(item.get("statement", "")).strip()
            if not statement:
                continue
            db.add(
                SourceClaim(
                    segment_id=segment.id,
                    statement=statement,
                    scope=str(item.get("scope") or heading or "general")[:200],
                    confidence=float(item.get("confidence", 0.8)),
                    fingerprint=fingerprint(statement),
                )
            )
    db.commit()
    db.refresh(document)
    return document, True


def compile_document(db: Session, document: SourceDocument) -> dict[str, int]:
    from .compiler import compile_claims

    claim_ids = [claim.id for segment in document.segments for claim in segment.claims]
    return compile_claims(db, claim_ids).__dict__ if claim_ids else {}
