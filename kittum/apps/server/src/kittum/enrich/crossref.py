"""Crossref: DOI resolution, retraction and correction notices, trial-number links, and the
publisher's full-text links.

Crossref has no OR filter for DOIs, so lookups are one request per DOI. The polite pool comes from
the `mailto` parameter. See docs/sources.md.

A publisher deposits text-mining links (`link`) and licences (`license`) per content version. A
PDF link counts as a full-text location only under a Creative Commons licence for its version (or
for text and data mining), so the waterfall's publisher step never fetches a closed article.
"""

import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlencode

from kittum.core import Record
from kittum.enrich._http import SourceUnavailable, fetch_json
from kittum.enrich._text import strip_markup
from kittum.enrich.types import OaLocation, OaVersion, WorkUpdate

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "crossref"
BASE = "https://api.crossref.org/works"
RETRACTION = "retraction"

type Direction = Literal["updates", "updated_by"]
type UpdateKind = tuple[str, Direction]

# Relation kinds stated by Crossref, mapped to the normalised notice kind and its direction.
# `is-retracted-by` is an incoming relation: the linked work retracts this one. A plain
# `retraction` relation and an `update-to` belong to the notice and point at the retracted work.
_RELATIONS: dict[str, UpdateKind] = {
    "retraction": (RETRACTION, "updates"),
    "is-retracted-by": (RETRACTION, "updated_by"),
    "correction": ("correction", "updates"),
    "is-correction-of": ("correction", "updates"),
    "erratum": ("erratum", "updates"),
    "is-erratum-of": ("erratum", "updates"),
    "expression_of_concern": ("expression_of_concern", "updates"),
    "is-expression-of-concern-of": ("expression_of_concern", "updates"),
}


class Work(Record):
    doi: str
    title: str | None = None
    abstract: str | None = None
    updates: tuple[WorkUpdate, ...] = ()
    clinical_trial_numbers: tuple[str, ...] = ()
    pdf_links: tuple[OaLocation, ...] = ()


def _url(doi: str, email: str | None) -> str:
    base = f"{BASE}/{quote(doi.lower(), safe='')}"
    if not email:
        return base
    return f"{base}?{urlencode({'mailto': email})}"


def _updates(message: dict[str, Any]) -> tuple[WorkUpdate, ...]:
    found: dict[tuple[str, str, str], WorkUpdate] = {}

    def add(doi: Any, kind: str, direction: Direction) -> None:
        if not isinstance(doi, str) or not doi or not kind:
            return
        update = WorkUpdate(doi=doi.lower(), kind=kind, direction=direction)
        found.setdefault((update.doi, update.kind, update.direction), update)

    for entry in message.get("update-to") or []:
        if isinstance(entry, dict):
            kind = entry.get("type")
            if isinstance(kind, str):
                add(entry.get("DOI"), _kind(kind), "updates")
    for entry in message.get("updated-by") or []:
        if isinstance(entry, dict):
            kind = entry.get("type")
            if isinstance(kind, str):
                add(entry.get("DOI"), _kind(kind), "updated_by")
    relation = message.get("relation")
    if isinstance(relation, dict):
        for kind, entries in relation.items():
            normalised, direction = _relation(kind)
            for entry in entries or []:
                if isinstance(entry, dict):
                    add(entry.get("id"), normalised, direction)
    return tuple(found.values())


def _kind(kind: str) -> str:
    """The normalised notice kind of one `update-to`/`updated-by` type."""
    return _RELATIONS.get(kind.lower(), (kind.lower(), "updates"))[0]


def _relation(kind: str) -> UpdateKind:
    """The notice kind and direction of a Crossref relation key."""
    return _RELATIONS.get(kind.lower(), (kind.lower(), "updates"))


def _trial_numbers(message: dict[str, Any]) -> tuple[str, ...]:
    numbers = []
    for entry in message.get("clinical-trial-number") or []:
        if isinstance(entry, dict):
            number = entry.get("clinical-trial-number")
            if isinstance(number, str) and number:
                numbers.append(number.upper())
    return tuple(dict.fromkeys(numbers))


_VERSIONS: dict[str, OaVersion] = {"vor": "published", "am": "accepted"}


def _open_licence(url: Any) -> str | None:
    """`cc-by-4.0` for a Creative Commons licence URL; None for any other licence."""
    if not isinstance(url, str):
        return None
    found = re.search(
        r"creativecommons\.org/(?:licenses/([a-z-]+)/([\d.]+)|publicdomain/zero)", url
    )
    if found is None:
        return None
    if found.group(1) is None:
        return "cc0"
    return f"cc-{found.group(1)}-{found.group(2)}"


def _pdf_links(message: dict[str, Any]) -> tuple[OaLocation, ...]:
    licences: dict[str, str] = {}
    for entry in message.get("license") or []:
        if isinstance(entry, dict) and (licence := _open_licence(entry.get("URL"))):
            licences.setdefault(str(entry.get("content-version") or "unspecified"), licence)
    found: dict[str, OaLocation] = {}
    for entry in message.get("link") or []:
        if not isinstance(entry, dict) or entry.get("content-type") != "application/pdf":
            continue
        url = entry.get("URL")
        version = str(entry.get("content-version") or "unspecified")
        licence = licences.get(version) or licences.get("tdm") or licences.get("unspecified")
        if not isinstance(url, str) or not url or licence is None:
            continue
        found.setdefault(
            url,
            OaLocation(
                url=url,
                licence=licence,
                version=_VERSIONS.get(version),
                source=SOURCE,
                format="pdf",
            ),
        )
    return tuple(found.values())


def work(gateway: Gateway, doi: str, *, email: str | None = None) -> Work | None:
    body = fetch_json(gateway, SOURCE, _url(doi, email))
    if body is None:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
        raise SourceUnavailable(SOURCE, "response has no work")
    message = body["message"]
    title = message.get("title")
    return Work(
        doi=(message.get("DOI") or doi).lower(),
        title=title[0] if isinstance(title, list) and title else None,
        abstract=strip_markup(message.get("abstract")),
        updates=_updates(message),
        clinical_trial_numbers=_trial_numbers(message),
        pdf_links=_pdf_links(message),
    )


def retraction_notice(work: Work) -> str | None:
    """The DOI of the notice that retracts this work, if Crossref records one.

    Only an incoming notice counts. Outgoing `update-to` links and a plain `retraction` relation
    belong to the notice itself, so a retraction notice is never flagged as retracted.
    """
    for update in work.updates:
        if update.kind == RETRACTION and update.direction == "updated_by":
            return update.doi
    return None
