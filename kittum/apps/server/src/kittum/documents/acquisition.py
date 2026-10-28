"""Fetching full text through the gateway and recording where it came from.

`acquire` walks the caller's waterfall of locations in order (docs/sources.md, "Full-text
waterfall") and keeps the first response that is a readable PDF. Every attempt goes into the
ledger event, so a failed waterfall is as visible as a successful one. A source that gives no
response at all (out of budget, unreachable) is an attempt too, and the waterfall moves on. Run it
as a job: it does network I/O.

A job that must keep each attempt even if a later one fails takes the steps one at a time:
`fetch_location` does the network work outside any transaction, and `record_location` records its
attempt in a transaction of the caller's, with an acquisition (`document.acquired`) when it got a
readable PDF and `document.acquisition_attempted` otherwise.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kittum.core import Clock, Id, NotFoundError, new_id
from kittum.documents.model import (
    Acquisition,
    AcquisitionAttempt,
    AcquisitionOutcome,
    FullTextLocation,
)
from kittum.documents.store import NotAPdf, get_document, ingest
from kittum.gateway import Gateway, HttpRequest
from kittum.ledger import EventDraft, Ledger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kittum.db import Tx
    from kittum.documents.model import Document
    from kittum.gateway import HttpResponse
    from kittum.storage import ArtifactStore

ACQUIRED = "document.acquired"
NOT_ACQUIRED = "document.acquisition_failed"
ATTEMPTED = "document.acquisition_attempted"


@dataclass(frozen=True)
class FetchedLocation:
    """One location as its source answered, before anything is recorded."""

    location: FullTextLocation
    response: HttpResponse | None
    error: str | None = None


def fetch_location(gateway: Gateway, location: FullTextLocation) -> FetchedLocation:
    """Ask one location for its PDF. Network only."""
    try:
        response = gateway.fetch(
            location.source,
            HttpRequest(url=location.url, headers=(("Accept", "application/pdf"),)),
        )
    except Exception as e:
        return FetchedLocation(location=location, response=None, error=f"{type(e).__name__}: {e}")
    return FetchedLocation(location=location, response=response)


def _attempt(
    tx: Tx, artifacts: ArtifactStore, fetched: FetchedLocation
) -> tuple[AcquisitionAttempt, Document | None]:
    location, response = fetched.location, fetched.response

    def attempt(outcome) -> AcquisitionAttempt:
        return AcquisitionAttempt(
            source=location.source,
            url=location.url,
            status=response.status if response else None,
            outcome=outcome,
            body_hash=response.body_hash if response and response.body else None,
            licence=location.licence,
            error=fetched.error,
        )

    if response is None:
        return attempt("unavailable"), None
    if response.status != 200:
        return attempt("http_error"), None
    if response.body.lstrip()[:5] != b"%PDF-":
        return attempt("not_pdf"), None
    try:
        document = ingest(tx, artifacts, response.body)
    except NotAPdf:
        return attempt("unreadable_pdf"), None
    return attempt("acquired"), document


def _acquisition(
    tx: Tx,
    *,
    ledger: Ledger,
    clock: Clock,
    project_id: Id,
    actor_id: Id,
    location: FullTextLocation,
    document: Document,
    attempts: Sequence[AcquisitionAttempt],
) -> Acquisition:
    acquisition = Acquisition(
        id=new_id(),
        project_id=project_id,
        document=document,
        source=location.source,
        url=location.url,
        licence=location.licence,
        terms_url=location.terms_url,
        acquired_at=clock.now(),
    )
    event = ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type=ACQUIRED,
            schema_version=1,
            actor_id=actor_id,
            payload={
                "acquisition_id": str(acquisition.id),
                "document_id": str(document.id),
                "artifact_hash": document.artifact_hash,
                "source": location.source,
                "url": location.url,
                "licence": location.licence,
                "terms_url": location.terms_url,
                "attempts": [a.model_dump(mode="json") for a in attempts],
            },
        ),
    )
    tx.execute(
        """insert into acquisition (id, project_id, document_id, source, url, licence,
                                    terms_url, acquired_at, event_id)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            acquisition.id,
            project_id,
            document.id,
            location.source,
            location.url,
            location.licence,
            location.terms_url,
            acquisition.acquired_at,
            event.event_id,
        ),
    )
    return acquisition


def record_location(
    tx: Tx,
    fetched: FetchedLocation,
    *,
    artifacts: ArtifactStore,
    ledger: Ledger,
    clock: Clock,
    project_id: Id,
    actor_id: Id,
) -> AcquisitionOutcome:
    """Record one fetched location: the acquisition when it is a readable PDF, else the failed
    attempt, each with its ledger event in `tx`."""
    attempt, document = _attempt(tx, artifacts, fetched)
    if document is None:
        ledger.append(
            tx,
            EventDraft(
                project_id=project_id,
                type=ATTEMPTED,
                schema_version=1,
                actor_id=actor_id,
                payload={"attempt": attempt.model_dump(mode="json")},
            ),
        )
        return AcquisitionOutcome(acquisition=None, attempts=(attempt,))
    acquisition = _acquisition(
        tx,
        ledger=ledger,
        clock=clock,
        project_id=project_id,
        actor_id=actor_id,
        location=fetched.location,
        document=document,
        attempts=(attempt,),
    )
    return AcquisitionOutcome(acquisition=acquisition, attempts=(attempt,))


def acquire(
    tx: Tx,
    *,
    gateway: Gateway,
    artifacts: ArtifactStore,
    ledger: Ledger,
    clock: Clock,
    project_id: Id,
    actor_id: Id,
    locations: Sequence[FullTextLocation],
) -> AcquisitionOutcome:
    attempts: list[AcquisitionAttempt] = []
    for location in locations:
        fetched = fetch_location(gateway, location)
        attempt, document = _attempt(tx, artifacts, fetched)
        attempts.append(attempt)
        if document is None:
            continue
        acquisition = _acquisition(
            tx,
            ledger=ledger,
            clock=clock,
            project_id=project_id,
            actor_id=actor_id,
            location=location,
            document=document,
            attempts=attempts,
        )
        return AcquisitionOutcome(acquisition=acquisition, attempts=tuple(attempts))
    ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type=NOT_ACQUIRED,
            schema_version=1,
            actor_id=actor_id,
            payload={"attempts": [a.model_dump(mode="json") for a in attempts]},
        ),
    )
    return AcquisitionOutcome(acquisition=None, attempts=tuple(attempts))


def acquisitions(tx: Tx, document_id: Id) -> list[dict[str, object]]:
    """Every recorded acquisition of a document, oldest first."""
    return tx.execute(
        "select * from acquisition where document_id = %s order by acquired_at, id", (document_id,)
    ).fetchall()


def get_acquisition(tx: Tx, acquisition_id: Id) -> Acquisition:
    row = tx.execute("select * from acquisition where id = %s", (acquisition_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"acquisition {acquisition_id} not found")
    return Acquisition(
        id=row["id"],
        project_id=row["project_id"],
        document=get_document(tx, row["document_id"]),
        source=row["source"],
        url=row["url"],
        licence=row["licence"],
        terms_url=row["terms_url"],
        acquired_at=row["acquired_at"],
    )
