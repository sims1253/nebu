"""Enrichment: abstracts, identifiers, open-access locations, retractions, entities.

Enrichment never counts toward PRISMA identification, so it may spread load across sources
("first healthy source"). Entity tags highlight text and never decide eligibility.

Owner: WP-06. Shipped: OpenAlex, Crossref (including `update-to` retractions,
`clinical-trial-number`, and openly licensed publisher PDF links), Europe PMC, Unpaywall, and PubTator 3 clients on `kittum.gateway`, stored
`record_enrichment` rows, and the full-text waterfall. Still to build: local tagging with the
OpenMed GLiNER models for documents that must stay local.
"""

from kittum.enrich.service import SOURCES, enrich, lookup
from kittum.enrich.store import store as save_enrichment
from kittum.enrich.types import (
    Enrichment,
    EnrichmentConfig,
    EntityTag,
    FieldSource,
    FullTextCandidate,
    OaLocation,
    RecordEnrichment,
    SourceFailure,
    WorkUpdate,
)
from kittum.enrich.waterfall import full_text_candidates

__all__ = [
    "SOURCES",
    "Enrichment",
    "EnrichmentConfig",
    "EntityTag",
    "FieldSource",
    "FullTextCandidate",
    "OaLocation",
    "RecordEnrichment",
    "SourceFailure",
    "WorkUpdate",
    "enrich",
    "full_text_candidates",
    "lookup",
    "save_enrichment",
]
