"""The full-text waterfall order from docs/sources.md.

Europe PMC full-text XML and PDF first, then PubMed Central's own copy, Unpaywall's best OA
location, OpenAlex OA locations, the publisher's own PDF links that Crossref lists under an open
licence, the preprint servers (medRxiv and bioRxiv, then arXiv), Semantic Scholar, and CORE. Only
file URLs count; landing pages are not download candidates, nor is a location whose licence takes
effect after `on`.

Enrichment finds the locations of Europe PMC, Unpaywall, OpenAlex, and Crossref for every record.
The other sources (`LOOKUP_SOURCES`) are asked one at a time by `lookup_full_text`, only for a
report that still has no full text when the waterfall reaches them.
"""

from typing import TYPE_CHECKING

from kittum.enrich import arxiv, biorxiv, core, pmc, semanticscholar
from kittum.enrich.types import FullTextCandidate, RecordEnrichment

if TYPE_CHECKING:
    from datetime import date

    from kittum.enrich.types import EnrichmentConfig, FullTextLookup, WorkIds
    from kittum.gateway import Gateway

WATERFALL = (
    "europepmc",
    pmc.SOURCE,
    "unpaywall",
    "openalex",
    "crossref",
    biorxiv.SOURCE,
    arxiv.SOURCE,
    semanticscholar.SOURCE,
    core.SOURCE,
)
"""Every full-text source, best first."""

LOOKUP_SOURCES = (pmc.SOURCE, biorxiv.SOURCE, arxiv.SOURCE, semanticscholar.SOURCE, core.SOURCE)
"""The sources the waterfall asks about one report when it reaches them."""

_SOURCE_ORDER = {source: rank for rank, source in enumerate(WATERFALL)}
_FORMAT_ORDER = {"xml": 0, "pdf": 1}


def waterfall_rank(source: str) -> int:
    """A source's place in the waterfall; an unknown source comes last."""
    return _SOURCE_ORDER.get(source, len(_SOURCE_ORDER))


def full_text_candidates(
    enrichment: RecordEnrichment, *, on: date
) -> tuple[FullTextCandidate, ...]:
    """List PDF and XML candidates for an enriched record on a day, best first, each with its
    licence."""
    ranked: list[tuple[int, int, FullTextCandidate]] = []
    for location in enrichment.oa_locations:
        if location.format not in {"pdf", "xml"}:
            continue
        if location.licence_start is not None and location.licence_start > on:
            continue
        ranked.append(
            (
                waterfall_rank(location.source),
                _FORMAT_ORDER[location.format],
                FullTextCandidate(
                    url=location.url,
                    format=location.format,
                    licence=location.licence,
                    licence_start=location.licence_start,
                    version=location.version,
                    source=location.source,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(candidate for _, _, candidate in ranked)


def skipped_source(source: str, config: EnrichmentConfig) -> str | None:
    """Why the waterfall leaves a lookup source out for every report, or `None` when it asks it."""
    if source == core.SOURCE and not config.core_api_key:
        return core.NO_KEY
    if source == semanticscholar.SOURCE and not config.semantic_scholar_api_key:
        return semanticscholar.NO_KEY
    return None


def lookup_applies(source: str, ids: WorkIds, *, config: EnrichmentConfig) -> bool:
    """Whether `lookup_full_text` would ask `source` about the work: it has an identifier the
    source takes, and the source is not skipped."""
    if skipped_source(source, config) is not None:
        return False
    match source:
        case pmc.SOURCE:
            return bool(ids.pmcid or ids.pmid or ids.doi)
        case biorxiv.SOURCE:
            return biorxiv.applies(ids)
        case arxiv.SOURCE:
            return arxiv.arxiv_id(ids) is not None
        case semanticscholar.SOURCE:
            return bool(ids.doi or ids.pmid)
        case core.SOURCE:
            return bool(ids.doi)
        case _:
            return False


def lookup_full_text(
    gateway: Gateway, source: str, ids: WorkIds, *, config: EnrichmentConfig
) -> FullTextLookup | None:
    """Ask one lookup source for a work's open files. `None` when the source cannot be asked about
    this work (no identifier it takes) or is skipped; raises `SourceUnavailable` when it fails."""
    match source:
        case pmc.SOURCE:
            return pmc.find(gateway, ids, email=config.contact_email)
        case biorxiv.SOURCE:
            return biorxiv.find(gateway, ids)
        case arxiv.SOURCE:
            return arxiv.find(gateway, ids)
        case semanticscholar.SOURCE:
            return semanticscholar.find(gateway, ids, api_key=config.semantic_scholar_api_key)
        case core.SOURCE:
            return core.find(gateway, ids, api_key=config.core_api_key)
        case _:
            raise ValueError(f"{source} is not a full-text lookup source")
