"""Enrichment: abstracts, identifiers, open-access locations, retractions, entities.

Enrichment never counts toward PRISMA identification, so it may spread load across sources
("first healthy source"). Entity tags highlight text and never decide eligibility.

Owner: WP-06. Shipped now: the types. Still to build: OpenAlex, Crossref (including
`update-to` retractions and `clinical-trial-number`), Europe PMC, Unpaywall, and PubTator 3
clients on `kittum.gateway`, plus local tagging with the OpenMed GLiNER models for documents that
must stay local.
"""

from typing import TYPE_CHECKING, Literal

from kittum.core import Record

if TYPE_CHECKING:
    from kittum.search import SourceRecord


class OaLocation(Record):
    url: str
    licence: str | None
    version: Literal["published", "accepted", "submitted"] | None = None


class EntityTag(Record):
    start: int
    end: int
    label: str  # e.g. "drug", "disease"
    normalized_id: str | None = None
    tagger: str


class RecordEnrichment(Record):
    provider_record_id: str
    doi: str | None = None
    pmid: str | None = None
    abstract: str | None = None
    retracted: bool = False
    retraction_notice_doi: str | None = None
    oa_locations: tuple[OaLocation, ...] = ()
    entities: tuple[EntityTag, ...] = ()
    sources_used: tuple[str, ...] = ()


class Enrichment(Record):
    records: tuple[RecordEnrichment, ...]


def enrich(records: tuple[SourceRecord, ...]) -> Enrichment:
    raise NotImplementedError("WP-06")


__all__ = ["Enrichment", "EntityTag", "OaLocation", "RecordEnrichment", "enrich"]
