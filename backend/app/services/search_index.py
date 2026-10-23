from __future__ import annotations

from typing import Any

from elasticsearch import Elasticsearch

from ..config import settings


class SearchIndex:
    """Best-effort Elasticsearch adapter; database retrieval remains the safe fallback."""

    index_name = "alg-qa-knowledge-v1"

    def __init__(self) -> None:
        self.enabled = settings.enable_elasticsearch
        self.client = (
            Elasticsearch(settings.elasticsearch_url, request_timeout=1, retry_on_timeout=False)
            if self.enabled
            else None
        )

    def index_segment(
        self, segment_id: str, document_id: str, title: str, heading: str | None, content: str
    ) -> None:
        if not self.client:
            return
        try:
            self.client.index(
                index=self.index_name,
                id=f"segment:{segment_id}",
                document={
                    "kind": "segment",
                    "segment_id": segment_id,
                    "document_id": document_id,
                    "title": title,
                    "heading": heading or "",
                    "content": content,
                },
                refresh=False,
            )
        except Exception:
            return

    def index_section(
        self, section_id: str, page_id: str, title: str, heading: str, content: str
    ) -> None:
        if not self.client:
            return
        try:
            self.client.index(
                index=self.index_name,
                id=f"section:{section_id}",
                document={
                    "kind": "section",
                    "section_id": section_id,
                    "page_id": page_id,
                    "title": title,
                    "heading": heading,
                    "content": content,
                },
                refresh=False,
            )
        except Exception:
            return

    def search(self, query: str, size: int = 10) -> list[dict[str, Any]]:
        if not self.client:
            return []
        try:
            response = self.client.search(
                index=self.index_name,
                size=size,
                query={
                    "multi_match": {
                        "query": query,
                        "fields": ["title^3", "heading^2", "content"],
                    }
                },
            )
            return [
                {**hit["_source"], "score": float(hit.get("_score") or 0)}
                for hit in response["hits"]["hits"]
            ]
        except Exception:
            return []


search_index = SearchIndex()
