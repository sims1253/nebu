"""PubTator 3: entity annotations for PMIDs, as BioC JSON.

Annotations are highlights, never eligibility decisions. PubTator's offsets are relative to its
own title and abstract passages, so callers re-anchor the annotation text in Kittum's title plus
abstract text (see `_text.title_and_abstract`).
"""

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from kittum.core import Record
from kittum.enrich._http import SourceUnavailable, chunks, unavailable
from kittum.gateway import HttpRequest, source_message

if TYPE_CHECKING:
    from kittum.gateway import Gateway

SOURCE = "pubtator"  # the gateway policy's name
BASE = "https://www.ncbi.nlm.nih.gov/research/pubtator3-api/publications/export/biocjson"

_LABELS = {
    "chemical": "drug",
    "disease": "disease",
    "gene": "gene",
    "mutation": "mutation",
    "species": "species",
    "cellline": "cell_line",
    "virus": "virus",
}


class Annotation(Record):
    """One annotation positioned relative to the start of its passage."""

    pmid: str
    passage: str  # "title" or "abstract"
    text: str
    offset: int  # BioC document offset minus the passage offset
    length: int
    label: str
    normalized_id: str | None = None


def _label(kind: str) -> str:
    return _LABELS.get(kind.lower(), kind.lower())


def _annotations(pmid: str, document: dict[str, Any]) -> list[Annotation]:
    found = []
    for passage in document.get("passages") or []:
        if not isinstance(passage, dict):
            continue
        infons = passage.get("infons") or {}
        passage_type = infons.get("type") if isinstance(infons, dict) else None
        passage_offset = passage.get("offset")
        if passage_type not in {"title", "abstract"}:
            continue
        if not isinstance(passage_offset, int):
            passage_offset = 0
        for annotation in passage.get("annotations") or []:
            if not isinstance(annotation, dict):
                continue
            text = annotation.get("text")
            annotation_infons = annotation.get("infons") or {}
            if not isinstance(text, str) or not isinstance(annotation_infons, dict):
                continue
            locations = annotation.get("locations") or []
            location = locations[0] if locations and isinstance(locations[0], dict) else {}
            offset = location.get("offset")
            length = location.get("length")
            if not isinstance(offset, int) or not isinstance(length, int) or length < 1:
                continue
            offset -= passage_offset
            if offset < 0:
                continue
            normalized = annotation_infons.get("identifier") or annotation_infons.get(
                "normalized_id"
            )
            found.append(
                Annotation(
                    pmid=pmid,
                    passage=passage_type,
                    text=text,
                    offset=offset,
                    length=length,
                    label=_label(str(annotation_infons.get("type") or "entity")),
                    normalized_id=normalized if isinstance(normalized, str) else None,
                )
            )
    return found


def annotations(
    gateway: Gateway, pmids: list[str] | tuple[str, ...], *, batch_size: int = 50
) -> dict[str, tuple[Annotation, ...]]:
    """Fetch annotations for many PMIDs. One request per batch."""
    found: dict[str, list[Annotation]] = {}
    values = [str(pmid) for pmid in dict.fromkeys(pmids) if pmid]
    for batch in chunks(values, batch_size):
        body = _fetch(gateway, ",".join(batch))
        for document in body:
            if not isinstance(document, dict):
                continue
            pmid = document.get("id")
            if not isinstance(pmid, str):
                continue
            annotations_here = _annotations(pmid, document)
            if annotations_here:
                found.setdefault(pmid, []).extend(annotations_here)
    return {pmid: tuple(items) for pmid, items in found.items()}


def _fetch(gateway: Gateway, pmids: str) -> list[Any]:
    url = f"{BASE}?{urlencode({'pmids': pmids})}"
    request = HttpRequest(url=url, headers=(("Accept", "application/json"),))
    try:
        response = gateway.fetch(SOURCE, request)
    except Exception as exc:
        raise unavailable(SOURCE, exc) from exc
    if response.status == 404:
        return []
    if response.status < 200 or response.status >= 300:
        raise SourceUnavailable(
            SOURCE,
            f"HTTP {response.status}",
            status=response.status,
            message=source_message(response.body),
        )
    try:
        body: Any = json.loads(response.body)
    except ValueError:
        body = _line_delimited(response.body)
        if body is None:
            raise SourceUnavailable(SOURCE, "response is not JSON", kind="malformed") from None
    if isinstance(body, dict):
        documents = body.get("PubTator3") or []
        return documents if isinstance(documents, list) else []
    if isinstance(body, list):
        return body
    raise SourceUnavailable(SOURCE, "unexpected response shape", kind="malformed")


def _line_delimited(payload: bytes) -> list[Any] | None:
    """NCBI sometimes streams one BioC document per line instead of one array."""
    documents: list[Any] = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        try:
            documents.append(json.loads(line))
        except ValueError:
            return None
    return documents or None
