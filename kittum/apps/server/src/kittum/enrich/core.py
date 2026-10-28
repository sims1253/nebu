"""CORE: a repository copy's PDF, by DOI. Only with a key (`Authorization: Bearer`).

CORE gives full texts to registered users only, so the waterfall skips it when no key is set and
says so. CORE states no licence for the copies it aggregates. See docs/sources.md.
"""

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from kittum.core import canonical_doi
from kittum.enrich._http import SourceUnavailable, fetch_json
from kittum.enrich.types import FullTextLookup, OaLocation, WorkIds

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "core"
BASE = "https://api.core.ac.uk/v3/search/works"
NO_KEY = "no CORE key is set (KITTUM_CORE_API_KEY)"


def find(gateway: Gateway, ids: WorkIds, *, api_key: str | None) -> FullTextLookup | None:
    doi = canonical_doi(ids.doi)
    if not doi or not api_key:
        return None
    url = f"{BASE}?{urlencode({'q': f'doi:"{doi}"', 'limit': '5'})}"
    body = fetch_json(gateway, SOURCE, url, headers=(("Authorization", f"Bearer {api_key}"),))
    if body is None:
        return FullTextLookup(source=SOURCE, url=url)
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise SourceUnavailable(SOURCE, "response has no results", kind="malformed", url=url)
    found = tuple(
        OaLocation(url=work["downloadUrl"], source=SOURCE, format="pdf")
        for work in body["results"]
        if isinstance(work, dict)
        and canonical_doi(work.get("doi") if isinstance(work.get("doi"), str) else None) == doi
        and isinstance(work.get("downloadUrl"), str)
        and work["downloadUrl"]
    )
    return FullTextLookup(source=SOURCE, url=url, locations=found)
