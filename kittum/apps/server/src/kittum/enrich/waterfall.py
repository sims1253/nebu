"""The full-text waterfall order from docs/sources.md.

Europe PMC full-text XML and PDF first, then Unpaywall's best OA location, then OpenAlex OA
locations, then the publisher's own PDF links that Crossref lists under an open licence. Only file
URLs count; landing pages are not download candidates.
"""

from kittum.enrich.types import FullTextCandidate, RecordEnrichment

_SOURCE_ORDER = {"europepmc": 0, "unpaywall": 1, "openalex": 2, "crossref": 3}
_FORMAT_ORDER = {"xml": 0, "pdf": 1}


def full_text_candidates(enrichment: RecordEnrichment) -> tuple[FullTextCandidate, ...]:
    """List PDF and XML candidates for an enriched record, best first, each with its licence."""
    ranked: list[tuple[int, int, FullTextCandidate]] = []
    for location in enrichment.oa_locations:
        if location.format not in {"pdf", "xml"}:
            continue
        ranked.append(
            (
                _SOURCE_ORDER.get(location.source, len(_SOURCE_ORDER)),
                _FORMAT_ORDER[location.format],
                FullTextCandidate(
                    url=location.url,
                    format=location.format,
                    licence=location.licence,
                    version=location.version,
                    source=location.source,
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(candidate for _, _, candidate in ranked)
