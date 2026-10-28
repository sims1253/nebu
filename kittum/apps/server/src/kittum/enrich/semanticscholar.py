"""Semantic Scholar: a paper's `openAccessPdf`, by DOI or PMID. Only with a key (`x-api-key`).

Semantic Scholar's API licence asks for permission before commercial use, so a deployment opts in
by setting its key; without one the waterfall skips it and says so. An answer whose DOI or PMID
names another work is refused. See docs/sources.md.
"""

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from kittum.core import canonical_doi
from kittum.enrich._http import SourceUnavailable, fetch_json
from kittum.enrich.types import FullTextLookup, OaLocation, WorkIds

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "semanticscholar"
BASE = "https://api.semanticscholar.org/graph/v1/paper"
NO_KEY = "no Semantic Scholar key is set (KITTUM_SEMANTIC_SCHOLAR_API_KEY)"

_CC = re.compile(r"^CC(BY)?(NC)?(ND)?(SA)?$")


def _licence(value: Any) -> str | None:
    """`CCBYNCND` as `cc-by-nc-nd`; anything else as Semantic Scholar wrote it."""
    if not isinstance(value, str) or not value:
        return None
    match = _CC.match(value.upper())
    if value.upper() == "CC0":
        return "cc0"
    if match and any(match.groups()):
        return "-".join(["cc", *(g.lower() for g in match.groups() if g)])
    return value


def find(gateway: Gateway, ids: WorkIds, *, api_key: str | None) -> FullTextLookup | None:
    if not api_key:
        return None
    doi = canonical_doi(ids.doi)
    if doi:
        paper = f"DOI:{doi}"
    elif ids.pmid:
        paper = f"PMID:{ids.pmid}"
    else:
        return None
    url = f"{BASE}/{quote(paper, safe=':/')}?{urlencode({'fields': 'externalIds,openAccessPdf'})}"
    body = fetch_json(gateway, SOURCE, url, headers=(("x-api-key", api_key),))
    if body is None:
        return FullTextLookup(source=SOURCE, url=url)
    if not isinstance(body, dict):
        raise SourceUnavailable(SOURCE, "unexpected response shape", kind="malformed", url=url)
    raw_ids = body.get("externalIds")
    external: dict[str, Any] = raw_ids if isinstance(raw_ids, dict) else {}
    their_doi = external.get("DOI")
    theirs_doi = canonical_doi(their_doi) if isinstance(their_doi, str) else None
    theirs_pmid = str(external["PubMed"]) if external.get("PubMed") else None
    for name, mine, theirs in (("DOI", doi, theirs_doi), ("PMID", ids.pmid, theirs_pmid)):
        if mine and theirs and mine != theirs:
            raise SourceUnavailable(
                SOURCE, f"answered for {name} {theirs}, not {mine}", kind="other_work", url=url
            )
    pdf = body.get("openAccessPdf")
    if not isinstance(pdf, dict) or not isinstance(pdf.get("url"), str) or not pdf["url"]:
        return FullTextLookup(source=SOURCE, url=url)
    return FullTextLookup(
        source=SOURCE,
        url=url,
        locations=(
            OaLocation(
                url=pdf["url"], licence=_licence(pdf.get("license")), source=SOURCE, format="pdf"
            ),
        ),
    )
