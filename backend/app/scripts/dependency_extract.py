"""Backfill DEPENDS_ON edges from existing claims.

Scans SourceClaim rows, only extracts from statements that mention service
dictionary terms (avoids calling the LLM for every claim). Idempotent: edges
are deduped by subject/object/predicate/claim_id, safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import SourceClaim
from app.services.graph import SERVICE_TERMS, extract_dependencies, graph_store


def run(db: Session) -> dict:
    scanned = 0
    extracted = 0
    written = 0
    skipped_low_conf = 0
    claims = db.scalars(select(SourceClaim)).all()
    for claim in claims:
        if not any(term.lower() in claim.statement.lower() for term in SERVICE_TERMS):
            continue
        scanned += 1
        dependencies = extract_dependencies(claim.statement)
        extracted += len(dependencies)
        for dependency in dependencies:
            if dependency["confidence"] < 0.8:
                skipped_low_conf += 1
            if graph_store().add_dependency(
                db,
                dependency["subject"],
                dependency["object"],
                claim_id=claim.id,
                confidence=dependency["confidence"],
                evidence=dependency.get("evidence", ""),
            ):
                written += 1
    db.commit()
    return {
        "total_claims": len(claims),
        "scanned": scanned,
        "extracted": extracted,
        "written": written,
        "skipped_low_conf": skipped_low_conf,
        "store": graph_store().name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill service dependency edges")
    parser.parse_args()
    with SessionLocal() as db:
        print(json.dumps(run(db), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
