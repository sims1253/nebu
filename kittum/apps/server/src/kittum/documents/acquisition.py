"""Fetching full text through the gateway and recording where it came from.

`acquire` walks the caller's waterfall of locations in order (docs/sources.md, "Full-text
waterfall") and keeps the first response that is a readable PDF. Every attempt goes into the
ledger event, so a failed waterfall is as visible as a successful one. A source that gives no
response at all (out of budget, unreachable) is an attempt too, and the waterfall moves on. Run it
as a job: it does network I/O.
"""

from typing import TYPE_CHECKING

from kittum.core import Clock, Id, new_id
from kittum.documents.model import (
    Acquisition,
    AcquisitionAttempt,
    AcquisitionOutcome,
    FullTextLocation,
)
from kittum.documents.store import NotAPdf, ingest
from kittum.gateway import Gateway, HttpRequest
from kittum.ledger import EventDraft, Ledger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kittum.db import Tx
    from kittum.storage import ArtifactStore

ACQUIRED = "document.acquired"
NOT_ACQUIRED = "document.acquisition_failed"


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
        try:
            response = gateway.fetch(
                location.source,
                HttpRequest(url=location.url, headers=(("Accept", "application/pdf"),)),
            )
        except Exception as e:
            attempts.append(
                AcquisitionAttempt(
                    source=location.source,
                    url=location.url,
                    status=None,
                    outcome="unavailable",
                    licence=location.licence,
                    error=f"{type(e).__name__}: {e}",
                )
            )
            continue

        def attempt(outcome, *, location=location, response=response) -> AcquisitionAttempt:
            return AcquisitionAttempt(
                source=location.source,
                url=location.url,
                status=response.status,
                outcome=outcome,
                body_hash=response.body_hash if response.body else None,
                licence=location.licence,
            )

        if response.status != 200:
            attempts.append(attempt("http_error"))
            continue
        if response.body.lstrip()[:5] != b"%PDF-":
            attempts.append(attempt("not_pdf"))
            continue
        try:
            document = ingest(tx, artifacts, response.body)
        except NotAPdf:
            attempts.append(attempt("unreadable_pdf"))
            continue
        attempts.append(attempt("acquired"))
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
