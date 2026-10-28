"""Unpaywall: the best open-access location for a DOI, with its licence.

Unpaywall needs an `email` parameter, so the source is skipped when no contact address is
configured. One request per DOI. See docs/sources.md.
"""

from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from kittum.core import canonical_doi
from kittum.enrich._http import SourceUnavailable, fetch_json
from kittum.enrich.types import OaLocation, OaVersion

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "unpaywall"
BASE = "https://api.unpaywall.org/v2"


def _url(doi: str, email: str) -> str:
    return f"{BASE}/{quote(canonical_doi(doi) or doi, safe='')}?{urlencode({'email': email})}"


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


def _location(raw: Any) -> OaLocation | None:
    if not isinstance(raw, dict):
        return None
    licence = raw.get("license") if isinstance(raw.get("license"), str) else None
    version = _version(raw.get("version"))
    pdf = raw.get("url_for_pdf")
    if isinstance(pdf, str) and pdf:
        return OaLocation(url=pdf, licence=licence, version=version, source=SOURCE, format="pdf")
    landing = raw.get("url_for_landing_page") or raw.get("url")
    if isinstance(landing, str) and landing:
        return OaLocation(
            url=landing, licence=licence, version=version, source=SOURCE, format="landing"
        )
    return None


def best_oa_location(gateway: Gateway, doi: str, *, email: str | None) -> OaLocation | None:
    if not email:
        return None
    body = fetch_json(gateway, SOURCE, _url(doi, email))
    if body is None:
        return None
    if not isinstance(body, dict):
        raise SourceUnavailable(SOURCE, "unexpected response shape")
    asked = canonical_doi(doi) or doi
    answered = canonical_doi(str(body.get("doi") or doi)) or asked
    if answered != asked:
        raise SourceUnavailable(SOURCE, f"answered for DOI {answered}, not {asked}")
    return _location(body.get("best_oa_location"))
