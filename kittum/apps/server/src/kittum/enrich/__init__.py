"""Enrichment: abstracts, identifiers, open-access locations, retractions, entities.

Enrichment never counts toward PRISMA identification, so it may spread load across sources
("first healthy source"). Entity tags highlight text and never decide eligibility.

Owner: WP-06. Shipped: OpenAlex, Crossref (including `update-to` retractions,
`clinical-trial-number`, and openly licensed publisher PDF links), Europe PMC, Unpaywall, and
PubTator 3 clients on `kittum.gateway`, stored `record_enrichment` rows, and the full-text
waterfall with its lookup sources (PubMed Central, medRxiv and bioRxiv, arXiv, Semantic Scholar,
CORE). Still to build: local tagging with the OpenMed GLiNER models for documents that must stay
local.
"""

from kittum.enrich._http import SourceUnavailable
from kittum.enrich.service import SOURCES, enrich, lookup
from kittum.enrich.store import store as save_enrichment
from kittum.enrich.types import (
    RETRYABLE,
    Enrichment,
    EnrichmentConfig,
    EntityTag,
    FailureKind,
    FieldSource,
    FullTextCandidate,
    FullTextLookup,
    OaLocation,
    RecordEnrichment,
    SourceFailure,
    WorkIds,
    WorkUpdate,
)
from kittum.enrich.waterfall import (
    LOOKUP_SOURCES,
    WATERFALL,
    full_text_candidates,
    lookup_applies,
    lookup_full_text,
    skipped_source,
    waterfall_rank,
)

__all__ = [
    "LOOKUP_SOURCES",
    "RETRYABLE",
    "SOURCES",
    "WATERFALL",
    "Enrichment",
    "EnrichmentConfig",
    "EntityTag",
    "FailureKind",
    "FieldSource",
    "FullTextCandidate",
    "FullTextLookup",
    "OaLocation",
    "RecordEnrichment",
    "SourceFailure",
    "SourceUnavailable",
    "WorkIds",
    "WorkUpdate",
    "enrich",
    "full_text_candidates",
    "lookup",
    "lookup_applies",
    "lookup_full_text",
    "save_enrichment",
    "skipped_source",
    "waterfall_rank",
]
