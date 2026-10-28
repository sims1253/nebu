"""The full-text waterfall order from docs/sources.md.

Europe PMC full-text XML and PDF first, then Unpaywall's best OA location, then OpenAlex OA
locations, then the publisher's own PDF links that Crossref lists under an open licence. Only file
URLs count; landing pages are not download candidates, nor is a location whose licence takes
effect after `on`.
"""

from typing import TYPE_CHECKING

from kittum.enrich.types import FullTextCandidate, RecordEnrichment

if TYPE_CHECKING:
    from datetime import date

_SOURCE_ORDER = {"europepmc": 0, "unpaywall": 1, "openalex": 2, "crossref": 3}
_FORMAT_ORDER = {"xml": 0, "pdf": 1}


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
                _SOURCE_ORDER.get(location.source, len(_SOURCE_ORDER)),
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
