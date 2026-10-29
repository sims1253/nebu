"""medRxiv and bioRxiv: a preprint's PDF and licence, by its DOI (10.1101/...).

Both servers share one API (`api.biorxiv.org/details/<server>/<doi>`) and one DOI prefix, so a
DOI is asked of medRxiv first and of bioRxiv when medRxiv has no such preprint. The newest version
answers. See docs/sources.md.
"""

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from kittum.core import canonical_doi
from kittum.enrich._http import SourceUnavailable, fetch_json
from kittum.enrich.types import FullTextLookup, OaLocation, WorkIds

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "biorxiv"
BASE = "https://api.biorxiv.org/details"
SERVERS = ("medrxiv", "biorxiv")
PREFIX = "10.1101/"

_LICENCES = {"cc_no": "no reuse licence", "cc0": "cc0"}


def applies(ids: WorkIds) -> bool:
    doi = canonical_doi(ids.doi)
    return bool(doi and doi.startswith(PREFIX))


def _licence(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _LICENCES.get(value, value.replace("_", "-"))


def find(gateway: Gateway, ids: WorkIds) -> FullTextLookup | None:
    doi = canonical_doi(ids.doi)
    if not doi or not doi.startswith(PREFIX):
        return None
    url = ""
    for server in SERVERS:
        url = f"{BASE}/{server}/{quote(doi, safe='/')}/na/json"
        body = fetch_json(gateway, SOURCE, url)
        if body is None:
            continue
        if not isinstance(body, dict) or not isinstance(body.get("collection"), list):
            raise SourceUnavailable(SOURCE, "response has no collection", kind="malformed", url=url)
        posts = [p for p in body["collection"] if isinstance(p, dict)]
        if not posts:
            continue
        newest = max(posts, key=lambda p: int(p.get("version") or 0))
        version = newest.get("version") or "1"
        return FullTextLookup(
            source=SOURCE,
            url=url,
            locations=(
                OaLocation(
                    url=f"https://www.{server}.org/content/{doi}v{version}.full.pdf",
                    licence=_licence(newest.get("license")),
                    version="submitted",
                    source=SOURCE,
                    format="pdf",
                ),
            ),
        )
    return FullTextLookup(source=SOURCE, url=url)
