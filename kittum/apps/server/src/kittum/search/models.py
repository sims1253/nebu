"""Core search models and protocol."""

from datetime import datetime
from typing import Literal, Protocol

from kittum.core import Id, Record, Sha256


class SearchPlan(Record):
    topic_id: str
    source: str  # e.g. "pubmed", "ctgov", "centralexport"
    platform: str  # e.g. "PubMed", "Cochrane Library (Wiley)"
    strategy: str
    limits: tuple[str, ...] = ()


class SourceRecord(Record):
    """One record as a source returned it, before dedupe.

    `occurrence_id` is assigned when the search module stores the execution; corpus and every
    later stage refer to records by it.
    """

    occurrence_id: Id
    source: str
    provider_record_id: str
    ordinal: int
    title: str
    abstract: str | None = None
    authors: tuple[str, ...] = ()
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    pmid: str | None = None
    registry_ids: tuple[str, ...] = ()
    publication_types: tuple[str, ...] = ()
    raw_hash: Sha256


class SearchExecution(Record):
    id: Id
    plan: SearchPlan
    kind: Literal["live_api", "file_import"]
    started_at: datetime
    ended_at: datetime
    query_translation: str | None
    reported_count: int
    retrieved_count: int
    raw_artifact_hashes: tuple[Sha256, ...]
    records: tuple[SourceRecord, ...]


class Source(Protocol):
    name: str

    def run(self, plan: SearchPlan) -> SearchExecution: ...


__all__ = [
    "SearchExecution",
    "SearchPlan",
    "Source",
    "SourceRecord",
]
