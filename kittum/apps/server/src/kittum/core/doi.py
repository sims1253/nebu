"""The one canonical form of a DOI, for every comparison and every lookup.

A DOI is case-insensitive and travels in many dresses: `https://doi.org/10.1/X`,
`http://dx.doi.org/10.1/x`, `doi.org/10.1/x`, `doi:10.1/x`, with stray whitespace. They name the
same work, so each is reduced to the bare, case-folded `10.1/x` before it is compared or sent.
"""

_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "https://www.doi.org/",
    "http://www.doi.org/",
    "doi.org/",
    "dx.doi.org/",
    "doi:",
)


def canonical_doi(value: str | None) -> str | None:
    """The bare, case-folded DOI, or None for an empty one."""
    if not value:
        return None
    doi = value.strip().casefold()
    for prefix in _PREFIXES:
        if doi.startswith(prefix):
            doi = doi[len(prefix) :].strip()
            break
    return doi or None
