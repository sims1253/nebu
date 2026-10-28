"""Europe PMC: search by PMID or DOI, full-text availability, and JATS abstracts.

`synonym=FALSE` keeps hit counts and lookups reproducible, as docs/sources.md requires. One search
can carry many IDs, so the whole record set costs one request per batch.
"""

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from kittum.core import Record
from kittum.enrich._http import SourceUnavailable, chunks, fetch_json
from kittum.enrich._text import strip_markup
from kittum.enrich.types import OaLocation

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "europepmc"
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
XML_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PDF_BASE = "https://europepmc.org/articles"


class Work(Record):
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    title: str | None = None
    abstract: str | None = None
    oa_locations: tuple[OaLocation, ...] = ()


def _url(query: str) -> str:
    params = urlencode(
        {
            "query": query,
            "format": "json",
            "resultType": "core",
            "synonym": "FALSE",
            "pageSize": "100",
        }
    )
    return f"{BASE}?{params}"


def _doi(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.removeprefix("https://doi.org/").lower()


def _locations(raw: dict[str, Any]) -> tuple[OaLocation, ...]:
    pmcid = raw.get("pmcid")
    in_epmc = raw.get("inEPMC") == "Y"
    licence = raw.get("license") if isinstance(raw.get("license"), str) else None
    if not in_epmc or not isinstance(pmcid, str) or not pmcid:
        return ()
    found = [
        OaLocation(
            url=f"{XML_BASE}/{pmcid}/fullTextXML",
            licence=licence,
            version="published",
            source=SOURCE,
            format="xml",
        )
    ]
    if raw.get("hasPDF") == "Y" or _has_pdf_link(raw):
        found.append(
            OaLocation(
                url=f"{PDF_BASE}/{pmcid}?pdf=render",
                licence=licence,
                version="published",
                source=SOURCE,
                format="pdf",
            )
        )
    return tuple(found)


def _has_pdf_link(raw: dict[str, Any]) -> bool:
    urls = raw.get("fullTextUrlList")
    if not isinstance(urls, dict):
        return False
    return any(
        isinstance(entry, dict)
        and entry.get("availabilityCode") == "OA"
        and entry.get("documentStyle") == "pdf"
        for entry in urls.get("fullTextUrl") or []
    )


def _work(raw: dict[str, Any]) -> Work | None:
    doi = _doi(raw.get("doi"))
    pmid = raw.get("pmid") if isinstance(raw.get("pmid"), str) else None
    if doi is None and pmid is None:
        return None
    pmcid = raw.get("pmcid") if isinstance(raw.get("pmcid"), str) else None
    return Work(
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        title=raw.get("title"),
        abstract=strip_markup(raw.get("abstractText")),
        oa_locations=_locations(raw),
    )


def works(
    gateway: Gateway,
    *,
    pmids: list[str] | tuple[str, ...] = (),
    dois: list[str] | tuple[str, ...] = (),
    batch_size: int = 50,
) -> list[Work]:
    pmid_values = [str(pmid) for pmid in dict.fromkeys(pmids) if pmid]
    doi_values = [doi.lower() for doi in dict.fromkeys(dois) if doi]
    terms = [f"EXT_ID:{pmid}" for pmid in pmid_values]
    terms += [f'DOI:"{doi}"' for doi in doi_values]
    found: list[Work] = []
    for batch in chunks(terms, batch_size):
        body = fetch_json(gateway, SOURCE, _url(" OR ".join(batch)))
        if body is None:
            continue
        if not isinstance(body, dict) or not isinstance(body.get("resultList"), dict):
            raise SourceUnavailable(SOURCE, "response has no result list")
        results = body["resultList"].get("result")
        if not isinstance(results, list):
            raise SourceUnavailable(SOURCE, "result list is not a list")
        found += [work for raw in results if isinstance(raw, dict) and (work := _work(raw))]
    return found
