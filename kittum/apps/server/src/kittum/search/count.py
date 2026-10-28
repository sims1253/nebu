"""Hit counts for a query, before any search runs (DESIGN.md, "Search").

A count is informational: it tells the author of a strategy how many records it would find today,
so an edit can be judged by its effect. It is not a `SearchExecution` and writes no ledger event;
the raw reply still goes to the artifact store, so a count shown to someone can be looked up.

    count = count_hits(gateway, artifacts, "pubmed", '"smoking cessation"[mh]')
"""

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, urlencode

import httpx2

from kittum.core import Clock, Record, Sha256, SystemClock
from kittum.gateway import (
    GatewayError,
    HttpRequest,
    SourceBudgetExhausted,
    SourceUnavailable,
)
from kittum.search.ctgov import CTGOV_BASE
from kittum.search.models import SourceError
from kittum.search.pubmed import EUTILS_BASE

if TYPE_CHECKING:
    from kittum.gateway import Gateway
    from kittum.storage import ArtifactStore

type QueryDialect = Literal["pubmed", "ctgov"]  # as `kittum.protocol.strategy_dialect` names them


class QueryCount(Record):
    dialect: QueryDialect
    strategy: str
    count: int
    query_translation: str | None = None
    """What PubMed made of the query, with its automatic term mapping."""
    warnings: tuple[str, ...] = ()
    """Words and quoted phrases PubMed did not find (usually a typo), words it ignored, fields it
    does not know, and its other notes, in the order it gave them."""
    raw_hash: Sha256
    counted_at: datetime


def count_hits(
    gateway: Gateway,
    artifacts: ArtifactStore,
    dialect: QueryDialect,
    strategy: str,
    *,
    contact_email: str | None = None,
    ncbi_api_key: str | None = None,
    clock: Clock | None = None,
) -> QueryCount:
    """How many records `strategy` finds now: PubMed's ESearch count or ClinicalTrials.gov's
    `totalCount`. Asks the source every time, past the response cache. Raises `SourceError` when
    the source refuses the query, and the gateway's `SourceUnavailable` when no answer came, a
    connection that failed or timed out included."""
    clock = clock or SystemClock()
    if not strategy.strip():
        raise SourceError("an empty query finds nothing to count")
    if dialect == "pubmed":
        params = {"db": "pubmed", "term": strategy, "retmax": "0", "tool": "kittum"}
        if contact_email:
            params["email"] = contact_email
        if ncbi_api_key:
            params["api_key"] = ncbi_api_key
        url = f"{EUTILS_BASE}/esearch.fcgi?{urlencode(params, quote_via=quote)}"
        source = "pubmed"
    else:
        params = {"query.term": strategy, "pageSize": "1", "countTotal": "true"}
        url = f"{CTGOV_BASE}?{urlencode(params, quote_via=quote)}"
        source = "clinicaltrials"  # the gateway policy's name
    try:
        response = gateway.fetch(source, HttpRequest(url=url), use_cache=False)
    except TRANSPORT_ERRORS as e:
        raise unreachable(source, e) from e
    raw_hash = artifacts.put(response.body)
    if not 200 <= response.status < 300:
        detail = response.body.decode(errors="replace")[:300]
        raise SourceError(
            f"{SOURCE_NAMES[source]} refused the query with HTTP {response.status}. {detail}"
        )
    if dialect == "pubmed":
        count, translation, warnings = _esearch_count(response.body)
    else:
        count, translation, warnings = _ctgov_count(response.body), None, ()
    return QueryCount(
        dialect=dialect,
        strategy=strategy,
        count=count,
        query_translation=translation,
        warnings=warnings,
        raw_hash=raw_hash,
        counted_at=clock.now(),
    )


# What ESearch says about the words of a query, in its ErrorList and WarningList: a word or quoted
# phrase it found nowhere (usually a typo), a word it left out, a field it does not know.
_NOTES = {
    "PhraseNotFound": "PubMed did not find the phrase",
    "QuotedPhraseNotFound": "PubMed did not find the quoted phrase",
    "PhraseIgnored": "PubMed ignored",
    "FieldNotFound": "PubMed does not know the field",
}


def _esearch_count(body: bytes) -> tuple[int, str | None, tuple[str, ...]]:
    root = ET.fromstring(body)
    error = root.findtext("ERROR")
    count = root.findtext("Count")
    if error or count is None:
        raise SourceError(f"PubMed could not count the query. {error or 'Its reply had no count.'}")
    warnings = [
        f"{_NOTES[e.tag]}: {(e.text or '').strip()}" if e.tag in _NOTES else (e.text or "").strip()
        for e in (*root.findall("ErrorList/*"), *root.findall("WarningList/*"))
    ]
    return int(count), root.findtext("QueryTranslation"), tuple(w for w in warnings if w)


def _ctgov_count(body: bytes) -> int:
    try:
        data = json.loads(body)
        return int(data["totalCount"])
    except (ValueError, KeyError, TypeError) as e:
        raise SourceError(f"ClinicalTrials.gov sent no count. {e}") from None


SOURCE_NAMES = {
    "pubmed": "PubMed",
    "medline": "PubMed",
    "clinicaltrials": "ClinicalTrials.gov",
    "ctgov": "ClinicalTrials.gov",
}
"""The names people know the live sources by, keyed by gateway policy and search plan source."""

TRANSPORT_ERRORS = (httpx2.TransportError, ConnectionError, TimeoutError)
"""What a transport raises when no response came: a refused or dropped connection, a timeout."""


def unreachable(source: str, error: BaseException) -> SourceUnavailable:
    """A request that got no response at all, as the gateway's `SourceUnavailable`, so a route
    answers it like every other unreachable source (502) and says why in plain words."""
    if isinstance(error, httpx2.TimeoutException | TimeoutError):
        reason = "it did not answer in time"
    elif isinstance(error, httpx2.ConnectError | ConnectionRefusedError):
        reason = "the connection failed"
    else:
        reason = "the connection broke off"
    return SourceUnavailable(source, None, 1, f"{reason} ({type(error).__name__})")


def failure_message(error: GatewayError | SourceError) -> str:
    """What a person reads when a live source did not answer or refused a query. A URL in it
    names its endpoint only: its query can hold the contact address or an API key."""
    return re.sub(r"(https?://[^\s?]+)\?\S*?(?=[.,]?(?:\s|$))", r"\1", _failure(error))


def _failure(error: GatewayError | SourceError) -> str:
    if isinstance(error, SourceUnavailable):
        name = SOURCE_NAMES.get(error.source, error.source)
        if error.reason:
            return f"Kittum could not reach {name}. {error.reason[:1].upper()}{error.reason[1:]}."
        return (
            f"Kittum could not reach {name}. It answered HTTP {error.status}"
            f" to {error.attempts} tries in a row."
        )
    if isinstance(error, SourceBudgetExhausted):
        name = SOURCE_NAMES.get(error.source, error.source)
        return f"Kittum has used today's {error.budget} requests to {name}. Try again tomorrow."
    return str(error)
