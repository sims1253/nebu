"""Crossref: DOI resolution, retraction and correction notices, and trial-number links.

Crossref has no OR filter for DOIs, so lookups are one request per DOI. The polite pool comes from
the `mailto` parameter. See docs/sources.md.
"""

from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlencode

from kittum.core import Record
from kittum.enrich._http import SourceUnavailable, fetch_json
from kittum.enrich._text import strip_markup
from kittum.enrich.types import WorkUpdate

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "crossref"
BASE = "https://api.crossref.org/works"
_RETRACTION_KINDS = {"retraction"}


class Work(Record):
    doi: str
    title: str | None = None
    abstract: str | None = None
    updates: tuple[WorkUpdate, ...] = ()
    clinical_trial_numbers: tuple[str, ...] = ()


def _url(doi: str, email: str | None) -> str:
    base = f"{BASE}/{quote(doi.lower(), safe='')}"
    if not email:
        return base
    return f"{base}?{urlencode({'mailto': email})}"


def _updates(message: dict[str, Any]) -> tuple[WorkUpdate, ...]:
    found: dict[tuple[str, str, str], WorkUpdate] = {}

    def add(doi: Any, kind: Any, direction: Literal["updates", "updated_by"]) -> None:
        if not isinstance(doi, str) or not doi or not isinstance(kind, str) or not kind:
            return
        update = WorkUpdate(doi=doi.lower(), kind=kind, direction=direction)
        found.setdefault((update.doi, update.kind, update.direction), update)

    for entry in message.get("update-to") or []:
        if isinstance(entry, dict):
            add(entry.get("DOI"), entry.get("type"), "updates")
    for entry in message.get("updated-by") or []:
        if isinstance(entry, dict):
            add(entry.get("DOI"), entry.get("type"), "updated_by")
    relation = message.get("relation")
    if isinstance(relation, dict):
        for kind, entries in relation.items():
            direction = "updated_by" if kind.startswith("is-") else "updates"
            for entry in entries or []:
                if isinstance(entry, dict):
                    add(entry.get("id"), kind, direction)
    return tuple(found.values())


def _trial_numbers(message: dict[str, Any]) -> tuple[str, ...]:
    numbers = []
    for entry in message.get("clinical-trial-number") or []:
        if isinstance(entry, dict):
            number = entry.get("clinical-trial-number")
            if isinstance(number, str) and number:
                numbers.append(number.upper())
    return tuple(dict.fromkeys(numbers))


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
    )


def retraction_notice(work: Work) -> str | None:
    """The DOI of the notice that retracts this work, if Crossref records one."""
    for update in work.updates:
        # `update-to` on a retracted paper is how some publishers deposit the notice; on a notice
        # it points the other way. Prefer `updated_by`, then accept `update-to`.
        if update.kind in _RETRACTION_KINDS and update.direction == "updated_by":
            return update.doi
    for update in work.updates:
        if update.kind in _RETRACTION_KINDS and update.direction == "updates":
            return update.doi
    return None
