"""OpenAlex: works by DOI and PMID in one OR filter, OA locations, inverted-index abstracts.

OpenAlex needs a free API key for any real volume; it goes in the `Authorization` header, so the
key never appears in a cached request URL. See docs/sources.md.
"""

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from kittum.core import Record
from kittum.enrich._http import SourceUnavailable, chunks, fetch_json
from kittum.enrich.types import OaLocation, OaVersion

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "openalex"
BASE = "https://api.openalex.org/works"
SELECT = (
    "id,doi,ids,display_name,abstract_inverted_index,is_retracted,"
    "open_access,best_oa_location,locations"
)


class Work(Record):
    doi: str | None = None
    pmid: str | None = None
    title: str | None = None
    abstract: str | None = None
    is_retracted: bool = False
    oa_locations: tuple[OaLocation, ...] = ()


def _headers(api_key: str | None) -> tuple[tuple[str, str], ...]:
    if not api_key:
        return ()
    return (("Authorization", f"Bearer {api_key}"),)


def _url(filter_value: str) -> str:
    params = urlencode(
        {"filter": filter_value, "per-page": "100", "select": SELECT},
        safe="|,:/",
    )
    return f"{BASE}?{params}"


def _strip_doi(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()


def _pmid(ids: dict[str, Any]) -> str | None:
    value = ids.get("pmid")
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str | None:
    """Rebuild the abstract text from OpenAlex's word-to-positions map."""
    if not index:
        return None
    words = sorted((position, word) for word, positions in index.items() for position in positions)
    return " ".join(word for _, word in words) or None


def _version(value: Any) -> OaVersion | None:
    match value:
        case "publishedVersion":
            return "published"
        case "acceptedVersion":
            return "accepted"
        case "submittedVersion":
            return "submitted"
        case _:
            return None


def _locations(work: dict[str, Any]) -> tuple[OaLocation, ...]:
    raw_locations = [work.get("best_oa_location"), *(work.get("locations") or [])]
    found: dict[str, OaLocation] = {}
    for raw in raw_locations:
        if not isinstance(raw, dict) or not raw.get("is_oa"):
            continue
        licence = raw.get("license")
        version = _version(raw.get("version"))
        pdf = raw.get("pdf_url")
        if isinstance(pdf, str) and pdf:
            found.setdefault(
                pdf,
                OaLocation(
                    url=pdf,
                    licence=licence,
                    version=version,
                    source=SOURCE,
                    format="pdf",
                ),
            )
            continue
        landing = raw.get("landing_page_url")
        if isinstance(landing, str) and landing:
            found.setdefault(
                landing,
                OaLocation(
                    url=landing,
                    licence=licence,
                    version=version,
                    source=SOURCE,
                    format="landing",
                ),
            )
    return tuple(found.values())


def _work(raw: dict[str, Any]) -> Work | None:
    doi = _strip_doi(raw.get("doi"))
    ids = raw.get("ids") or {}
    pmid = _pmid(ids) if isinstance(ids, dict) else None
    if doi is None and pmid is None:
        return None
    abstract = abstract_from_inverted_index(raw.get("abstract_inverted_index"))
    return Work(
        doi=doi,
        pmid=pmid,
        title=raw.get("display_name"),
        abstract=abstract,
        is_retracted=bool(raw.get("is_retracted")),
        oa_locations=_locations(raw),
    )


def _fetch(gateway: Gateway, api_key: str | None, filter_value: str) -> list[Work]:
    body = fetch_json(gateway, SOURCE, _url(filter_value), headers=_headers(api_key))
    if body is None:
        return []
    if not isinstance(body, dict):
        raise SourceUnavailable(SOURCE, "unexpected response shape")
    results = body.get("results")
    if not isinstance(results, list):
        raise SourceUnavailable(SOURCE, "response has no results")
    return [work for raw in results if isinstance(raw, dict) and (work := _work(raw))]


def works(
    gateway: Gateway,
    *,
    dois: list[str] | tuple[str, ...] = (),
    pmids: list[str] | tuple[str, ...] = (),
    api_key: str | None = None,
    batch_size: int = 50,
) -> list[Work]:
    """Look works up in batches. DOI and PMID filters cannot be combined, so one request each."""
    found: list[Work] = []
    doi_values = [doi.lower() for doi in dict.fromkeys(dois) if doi]
    for batch in chunks(doi_values, batch_size):
        found += _fetch(gateway, api_key, "doi:" + "|".join(batch))
    pmid_values = [str(pmid) for pmid in dict.fromkeys(pmids) if pmid]
    for batch in chunks(pmid_values, batch_size):
        found += _fetch(gateway, api_key, "pmid:" + "|".join(batch))
    return found
