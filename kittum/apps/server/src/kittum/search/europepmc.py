"""Europe PMC REST search source adapter (DESIGN.md, "Search"; docs/sources.md).

Runs the query with `synonym=FALSE`, so Europe PMC searches only the words written and a count is
reproducible, and pages through every result with `cursorMark`. Each result maps to a
SourceRecord with its PMID, PMCID and DOI, so deduplication can match it to the same paper found
on PubMed.
"""

import html
import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from kittum.core import Clock, Sha256, SystemClock, new_id
from kittum.gateway import HttpRequest
from kittum.search.models import SearchExecution, SearchPlan, SourceError, SourceRecord
from kittum.search.pubmed import EUDRACT_PATTERN, NCT_PATTERN

if TYPE_CHECKING:
    from kittum.gateway import Gateway
    from kittum.storage import ArtifactStore

EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE_SIZE = 1000  # the most Europe PMC returns per page

_TAG = re.compile(r"<[^>]+>")
_HEADING = re.compile(r"<h4>(.*?)</h4>", re.S)


def _text(value: str | None) -> str:
    """Europe PMC's HTML title or abstract as plain text."""
    return " ".join(html.unescape(_TAG.sub(" ", value or "")).split())


def _abstract(value: str | None) -> str | None:
    """A structured abstract as PubMed's are stored: `Label: text` sections, a blank line apart."""
    if not value:
        return None
    parts = _HEADING.split(value)
    sections = [_text(parts[0])] if _text(parts[0]) else []
    for label, body in zip(parts[1::2], parts[2::2], strict=True):
        text = _text(body)
        if text:
            sections.append(f"{_text(label)}: {text}" if _text(label) else text)
    return "\n\n".join(sections) or None


def _authors(result: dict[str, Any]) -> tuple[str, ...]:
    listed = (result.get("authorList") or {}).get("author") or []
    names = [
        (a.get("fullName") or a.get("collectiveName") or "").strip()
        for a in listed
        if isinstance(a, dict)
    ]
    if not any(names):
        names = [n.strip() for n in (result.get("authorString") or "").rstrip(".").split(",")]
    return tuple(n for n in names if n)


def parse_result(result: dict[str, Any], raw_hash: Sha256, ordinal: int) -> SourceRecord:
    """One Europe PMC `core` result as a SourceRecord. Trial registry IDs come from the title and
    abstract, as for PubMed: Europe PMC's results say that an article names an NCT ID, not which."""
    title = _text(result.get("title"))
    abstract = _abstract(result.get("abstractText"))
    year = result.get("pubYear")
    journal = ((result.get("journalInfo") or {}).get("journal") or {}).get("title")
    text = f"{title} {abstract or ''}"
    registry_ids = [n.upper() for n in NCT_PATTERN.findall(text)] + EUDRACT_PATTERN.findall(text)
    return SourceRecord(
        occurrence_id=new_id(),
        source="europepmc",
        provider_record_id=f"{result.get('source', '')}:{result.get('id', '')}",
        ordinal=ordinal,
        title=title,
        abstract=abstract,
        authors=_authors(result),
        year=int(year) if isinstance(year, str) and year.isdigit() else None,
        journal=journal,
        doi=result.get("doi") or None,
        pmid=result.get("pmid") or None,
        pmcid=result.get("pmcid") or None,
        registry_ids=tuple(dict.fromkeys(registry_ids)),
        publication_types=tuple((result.get("pubTypeList") or {}).get("pubType") or ()),
        raw_hash=raw_hash,
    )


def search_url(query: str, *, cursor: str = "*", page_size: int = PAGE_SIZE) -> str:
    params = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "synonym": "FALSE",
        "pageSize": str(page_size),
        "cursorMark": cursor,
    }
    return f"{EUROPEPMC_SEARCH}?{urlencode(params, quote_via=quote)}"


class EuropePmcSource:
    """Live API source adapter for the Europe PMC REST search."""

    name: str = "europepmc"

    def __init__(
        self,
        gateway: Gateway,
        artifacts: ArtifactStore,
        *,
        clock: Clock | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._gateway = gateway
        self._artifacts = artifacts
        self._clock = clock or SystemClock()
        self._page_size = page_size

    def run(self, plan: SearchPlan) -> SearchExecution:
        started_at = self._clock.now()
        raw_hashes: list[Sha256] = []
        records: dict[str, SourceRecord] = {}
        reported: int | None = None
        translation: str | None = None
        cursor = "*"
        while True:
            url = search_url(plan.strategy, cursor=cursor, page_size=self._page_size)
            resp = self._gateway.fetch("europepmc", HttpRequest(url=url))
            if not 200 <= resp.status < 300:
                raise SourceError(f"Europe PMC returned HTTP {resp.status} for {url}")
            body_hash = self._artifacts.put(resp.body)
            raw_hashes.append(body_hash)
            try:
                data = json.loads(resp.body)
                results = data["resultList"]["result"] if "resultList" in data else []
                if reported is None:
                    reported = int(data["hitCount"])
                    request = data.get("request") or {}
                    # What Europe PMC says it ran: the query and whether it added synonyms.
                    translation = (
                        f"{request.get('internalQuery') or request.get('queryString')}\n"
                        f"synonym={'TRUE' if request.get('synonym') else 'FALSE'}"
                    )
                elif "hitCount" in data and int(data["hitCount"]) != reported:
                    # The result changed while Kittum paged through it: no page set is complete.
                    raise SourceError(
                        f"Europe PMC's count changed from {reported} to {data['hitCount']}"
                        " during the search. Run it again."
                    )
            except (ValueError, KeyError, TypeError) as e:
                raise SourceError(f"Europe PMC sent a reply Kittum cannot read: {e}") from None
            for result in results:
                record = parse_result(result, body_hash, len(records))
                records.setdefault(record.provider_record_id, record)
            next_cursor = data.get("nextCursorMark")
            if not results or len(records) >= reported or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        assert reported is not None
        if len(records) != reported:
            raise SourceError(f"Europe PMC reported {reported} records but returned {len(records)}")
        return SearchExecution(
            id=new_id(),
            plan=plan,
            kind="live_api",
            started_at=started_at,
            ended_at=self._clock.now(),
            query_translation=translation,
            reported_count=reported,
            retrieved_count=len(records),
            raw_artifact_hashes=tuple(raw_hashes),
            records=tuple(
                r.model_copy(update={"ordinal": i}) for i, r in enumerate(records.values())
            ),
        )
