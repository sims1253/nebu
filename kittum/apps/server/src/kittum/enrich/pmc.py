"""PubMed Central: the PMC ID converter, then the PMC Article Datasets on AWS.

PMC retired its OA web service (`oa.fcgi`) in August 2026. Its replacement for automated
retrieval is the world-readable `pmc-oa-opendata` bucket, which holds a JSON metadata object per
article version with its licence and, when the licence allows it, a PDF. A record without a PMCID
gets one from the ID converter by PMID or DOI. The bucket is listed for the PMCID's versions, and
each version's metadata names its PDF. The published version comes before an author manuscript.
See docs/sources.md.
"""

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlsplit

from kittum.core import canonical_doi
from kittum.enrich._http import SourceUnavailable, fetch, fetch_json
from kittum.enrich.types import FullTextLookup, OaLocation, WorkIds

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "pmc"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"

_KEY = re.compile(rb"<Key>(metadata/PMC\d+\.\d+\.json)</Key>")


def _idconv_url(ids: WorkIds, email: str | None) -> str | None:
    if ids.pmid:
        params = {"ids": ids.pmid, "idtype": "pmid"}
    elif ids.doi:
        params = {"ids": ids.doi, "idtype": "doi"}
    else:
        return None
    params |= {"format": "json", "tool": "kittum"}
    if email:
        params["email"] = email
    return f"{IDCONV}?{urlencode(params)}"


def _pmcid(gateway: Gateway, url: str, ids: WorkIds) -> str | None:
    body = fetch_json(gateway, SOURCE, url)
    if not isinstance(body, dict) or not isinstance(body.get("records"), list):
        raise SourceUnavailable(
            SOURCE, "the ID converter sent no records", kind="malformed", url=url
        )
    for record in body["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("pmcid"), str):
            continue
        _check(record, ids, url)
        return record["pmcid"]
    return None


def _check(found: dict[str, Any], ids: WorkIds, url: str) -> None:
    """Refuse an answer whose DOI or PMID names another work."""
    doi = canonical_doi(found["doi"]) if isinstance(found.get("doi"), str) else None
    pmid = str(found["pmid"]) if found.get("pmid") else None
    for name, mine, theirs in (("DOI", canonical_doi(ids.doi), doi), ("PMID", ids.pmid, pmid)):
        if mine and theirs and mine != theirs:
            raise SourceUnavailable(
                SOURCE, f"answered for {name} {theirs}, not {mine}", kind="other_work", url=url
            )


def _https(s3_url: str) -> str:
    """`s3://pmc-oa-opendata/KEY?md5=...` as the bucket's HTTPS address, without the digest."""
    split = urlsplit(s3_url)
    return f"{BUCKET}{split.path}"


def find(gateway: Gateway, ids: WorkIds, *, email: str | None = None) -> FullTextLookup | None:
    """The PDFs PMC holds for a work, best version first. `None` when there is nothing to ask by."""
    pmcid = ids.pmcid.upper() if ids.pmcid else None
    asked = _idconv_url(ids, email) if pmcid is None else None
    if pmcid is None:
        if asked is None:
            return None
        pmcid = _pmcid(gateway, asked, ids)
        if pmcid is None:
            return FullTextLookup(source=SOURCE, url=asked)
    listing = f"{BUCKET}/?{urlencode({'list-type': '2', 'prefix': f'metadata/{pmcid}.'})}"
    asked = asked or listing
    body = fetch(gateway, SOURCE, listing, accept="application/xml")
    keys = _KEY.findall(body or b"")
    versions: list[dict[str, Any]] = []
    for key in keys:
        url = f"{BUCKET}/{quote(key.decode())}"
        meta = fetch_json(gateway, SOURCE, url)
        if isinstance(meta, dict):
            _check(meta, ids, url)
            versions.append(meta)
    # The published version first, then the newest.
    versions.sort(key=lambda m: (bool(m.get("is_manuscript")), -int(m.get("version") or 0)))
    locations = tuple(
        OaLocation(
            url=_https(meta["pdf_url"]),
            licence=meta.get("license_code") if isinstance(meta.get("license_code"), str) else None,
            version="accepted" if meta.get("is_manuscript") else "published",
            source=SOURCE,
            format="pdf",
        )
        for meta in versions
        if isinstance(meta.get("pdf_url"), str) and meta["pdf_url"].startswith("s3://")
    )
    return FullTextLookup(source=SOURCE, url=asked, locations=locations)
