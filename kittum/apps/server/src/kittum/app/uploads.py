"""Many full texts at once: the lead uploads the PDFs a customer sent, and Kittum matches each one.

    result = upload_full_text(tx, project_id, "10.2196_66709.pdf", pdf, actor_id=lead, ...)
    # attached to its report, or held for the lead with the reports it could be
    assign_upload(tx, project_id, result.upload_id, report_id, actor_id=lead, ...)

A PDF matches the reports still without a usable full text (those sought for retrieval, or every
report before any is screened). Its file name decides first: a report's DOI in it (with `/` as
`_`, `-`, or `%2F`, the way people name files) or its PMID as a whole number. Otherwise its first
pages decide, by the same check the waterfall makes (`kittum.documents.check_pages`: the report's
DOI or title). A PDF that one report matches becomes its full text, recorded like a single upload
(`project.full_text_attached`). One the file name gives to a report whose DOI and title its pages
do not show, one that matches several reports, and one that matches none are held: the lead
assigns each by hand, or discards it. Every upload, held or attached, is a ledger event.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import unquote
from uuid import UUID

from psycopg.types.json import Jsonb

from kittum.app.lifecycle import FullText, LifecycleError, attach_full_text, full_text
from kittum.app.open_access import report_facts, without_full_text
from kittum.core import Clock, Id, NotFoundError, Record, new_id
from kittum.documents import check_pages, first_pages, ingest
from kittum.ledger import EventDraft

if TYPE_CHECKING:
    from kittum.app.open_access import ReportFacts
    from kittum.db import Tx
    from kittum.ledger import Ledger
    from kittum.screening import Screening
    from kittum.storage import ArtifactStore

UPLOAD_MATCHED = "retrieval.upload_matched"
UPLOAD_HELD = "retrieval.upload_held"
UPLOAD_ASSIGNED = "retrieval.upload_assigned"
UPLOAD_DISCARDED = "retrieval.upload_discarded"

NO_MATCH = "Neither its file name nor its first pages match a report without a PDF."
NAME_CONTRADICTED = (
    "Its file name names a report, but its first pages show neither that report's DOI nor its"
    " title."
)


class UploadCandidate(Record):
    report_id: Id
    title: str
    doi: str | None
    pmid: str | None


class UploadResult(Record):
    upload_id: Id
    file_name: str
    status: Literal["attached", "held"]
    report_id: Id | None = None
    """The report it became the full text of."""
    matched_by: Literal["file_name", "content"] | None = None
    reason: str | None = None
    """Why it is held."""
    candidates: tuple[UploadCandidate, ...] = ()
    page_count: int | None = None


class HeldUpload(Record):
    upload_id: Id
    file_name: str
    uploaded_at: datetime
    document_id: Id
    page_count: int | None
    reason: str
    candidates: tuple[UploadCandidate, ...]


def _event(project_id: Id, type: str, actor_id: Id, payload: dict[str, object]) -> EventDraft:
    return EventDraft(
        project_id=project_id, type=type, schema_version=1, actor_id=actor_id, payload=payload
    )


def _candidates(tx: Tx, project_id: Id, screening: Screening) -> list[Id]:
    sought = without_full_text(tx, project_id, screening)
    if sought:
        return sought
    return [r for r in report_facts(tx, project_id) if full_text(tx, project_id, str(r)) is None]


def _named(file_name: str, facts: ReportFacts) -> bool:
    name = unquote(file_name).casefold()
    if facts.doi:
        doi = facts.doi.casefold()
        for spelling in dict.fromkeys((doi, doi.replace("/", "_"), doi.replace("/", "-"))):
            if re.search(rf"(?<![a-z0-9]){re.escape(spelling)}(?![a-z0-9])", name):
                return True
    return bool(facts.pmid and re.search(rf"(?<!\d){re.escape(facts.pmid)}(?!\d)", name))


def _candidate(report_id: Id, facts: ReportFacts) -> UploadCandidate:
    return UploadCandidate(report_id=report_id, title=facts.title, doi=facts.doi, pmid=facts.pmid)


@dataclass(frozen=True)
class UploadPlan:
    """Where one PDF of a bulk upload goes, decided before anything is written."""

    target: Id | None
    matched_by: Literal["file_name", "content"] | None
    held: tuple[Id, ...]
    reason: str | None


def plan_upload(
    tx: Tx,
    project_id: Id,
    file_name: str,
    pages: tuple[str, ...] | None,
    *,
    screening: Screening,
) -> UploadPlan:
    """Match a PDF's file name and first pages (`kittum.documents.first_pages`) to the reports
    still without a full text. Reads only, so the caller can match before it writes."""
    facts = report_facts(tx, project_id)
    candidates = [r for r in _candidates(tx, project_id, screening) if r in facts]
    named = [r for r in candidates if _named(file_name, facts[r])]
    if len(named) == 1:
        check = check_pages(pages, doi=facts[named[0]].doi, title=facts[named[0]].title)
        if check.verdict == "mismatch":
            return UploadPlan(None, None, tuple(named), NAME_CONTRADICTED)
        return UploadPlan(named[0], "file_name", (), None)
    if named:
        return UploadPlan(None, None, tuple(named), f"Its file name fits {len(named)} reports.")
    shown = [
        r
        for r in candidates
        if check_pages(pages, doi=facts[r].doi, title=facts[r].title).verdict == "match"
    ]
    if len(shown) == 1:
        return UploadPlan(shown[0], "content", (), None)
    if shown:
        return UploadPlan(None, None, tuple(shown), f"Its first pages fit {len(shown)} reports.")
    return UploadPlan(None, None, (), NO_MATCH)


def upload_full_text(
    tx: Tx,
    project_id: Id,
    file_name: str,
    pdf: bytes,
    *,
    actor_id: Id,
    screening: Screening,
    artifacts: ArtifactStore,
    ledger: Ledger,
    clock: Clock,
    licence: str | None = None,
    plan: UploadPlan | None = None,
) -> UploadResult:
    """Store one PDF of a bulk upload and attach it to the report it matches, or hold it.
    Raises `kittum.documents.NotAPdf` for a file that is not a PDF. With a `plan` made before the
    transaction (`plan_upload`), the PDF goes where it says, unless its report got a full text
    meanwhile; then it is matched again."""
    document = ingest(tx, artifacts, pdf)
    if plan is None or (
        plan.target is not None and full_text(tx, project_id, str(plan.target)) is not None
    ):
        plan = plan_upload(tx, project_id, file_name, first_pages(pdf), screening=screening)
    facts = report_facts(tx, project_id)
    target, matched_by, reason = plan.target, plan.matched_by, plan.reason
    held = [r for r in plan.held if r in facts]
    upload_id = new_id()
    if target is not None:
        event = ledger.append(
            tx,
            _event(
                project_id,
                UPLOAD_MATCHED,
                actor_id,
                {
                    "upload_id": str(upload_id),
                    "file_name": file_name,
                    "document_id": str(document.id),
                    "report_id": str(target),
                    "matched_by": matched_by,
                },
            ),
        )
        attached = attach_full_text(
            tx,
            project_id,
            str(target),
            pdf,
            actor_id=actor_id,
            artifacts=artifacts,
            ledger=ledger,
            licence=licence,
        )
        _insert(
            tx,
            upload_id,
            project_id,
            document.id,
            file_name,
            licence,
            actor_id,
            clock,
            status="attached",
            matched_by=matched_by,
            item_ref=str(target),
            event_id=event.event_id,
        )
        return UploadResult(
            upload_id=upload_id,
            file_name=file_name,
            status="attached",
            report_id=target,
            matched_by=matched_by,
            page_count=attached.page_count,
        )
    event = ledger.append(
        tx,
        _event(
            project_id,
            UPLOAD_HELD,
            actor_id,
            {
                "upload_id": str(upload_id),
                "file_name": file_name,
                "document_id": str(document.id),
                "reason": reason,
                "candidates": [str(r) for r in held],
            },
        ),
    )
    _insert(
        tx,
        upload_id,
        project_id,
        document.id,
        file_name,
        licence,
        actor_id,
        clock,
        status="held",
        reason=reason,
        candidates=held,
        event_id=event.event_id,
    )
    return UploadResult(
        upload_id=upload_id,
        file_name=file_name,
        status="held",
        reason=reason,
        candidates=tuple(_candidate(r, facts[r]) for r in held),
        page_count=document.page_count,
    )


def _insert(
    tx: Tx,
    upload_id: Id,
    project_id: Id,
    document_id: Id,
    file_name: str,
    licence: str | None,
    actor_id: Id,
    clock: Clock,
    *,
    status: str,
    event_id: Id,
    matched_by: str | None = None,
    reason: str | None = None,
    candidates: list[Id] | None = None,
    item_ref: str | None = None,
) -> None:
    tx.execute(
        "insert into retrieval_upload (id, project_id, document_id, file_name, licence,"
        " uploaded_by, uploaded_at, status, matched_by, reason, candidates, item_ref, event_id)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            upload_id,
            project_id,
            document_id,
            file_name,
            licence,
            actor_id,
            clock.now(),
            status,
            matched_by,
            reason,
            Jsonb([str(c) for c in candidates or []]),
            item_ref,
            event_id,
        ),
    )


def held_uploads(tx: Tx, project_id: Id) -> list[HeldUpload]:
    """The uploads waiting for the lead, oldest first."""
    facts = report_facts(tx, project_id)
    rows = tx.execute(
        "select u.*, d.page_count from retrieval_upload u join document d on d.id = u.document_id"
        " where u.project_id = %s and u.status = 'held' order by u.uploaded_at, u.id",
        (project_id,),
    ).fetchall()
    out = []
    for row in rows:
        ids = [c for c in row["candidates"] if UUID(c) in facts]
        out.append(
            HeldUpload(
                upload_id=row["id"],
                file_name=row["file_name"],
                uploaded_at=row["uploaded_at"],
                document_id=row["document_id"],
                page_count=row["page_count"],
                reason=row["reason"] or NO_MATCH,
                candidates=tuple(_candidate(UUID(c), facts[UUID(c)]) for c in ids),
            )
        )
    return out


def _held(tx: Tx, project_id: Id, upload_id: Id) -> dict[str, Any]:
    row = tx.execute(
        "select * from retrieval_upload where project_id = %s and id = %s for update",
        (project_id, upload_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"upload {upload_id} not found")
    if row["status"] != "held":
        raise LifecycleError(f"upload {upload_id} is {row['status']} already")
    return row


def assign_upload(
    tx: Tx,
    project_id: Id,
    upload_id: Id,
    report_id: Id,
    *,
    actor_id: Id,
    artifacts: ArtifactStore,
    ledger: Ledger,
) -> FullText:
    """Make a held upload the full text of the report the lead chose."""
    row = _held(tx, project_id, upload_id)
    if report_id not in report_facts(tx, project_id):
        raise NotFoundError(f"report {report_id} is not in the corpus")
    document = tx.execute(
        "select artifact_hash from document where id = %s", (row["document_id"],)
    ).fetchone()
    assert document is not None
    ledger.append(
        tx,
        _event(
            project_id,
            UPLOAD_ASSIGNED,
            actor_id,
            {
                "upload_id": str(upload_id),
                "file_name": row["file_name"],
                "report_id": str(report_id),
            },
        ),
    )
    attached = attach_full_text(
        tx,
        project_id,
        str(report_id),
        artifacts.get(document["artifact_hash"]),
        actor_id=actor_id,
        artifacts=artifacts,
        ledger=ledger,
        licence=row["licence"],
    )
    tx.execute(
        "update retrieval_upload set status = 'assigned', matched_by = 'lead', item_ref = %s"
        " where id = %s",
        (str(report_id), upload_id),
    )
    return attached


def discard_upload(tx: Tx, project_id: Id, upload_id: Id, *, actor_id: Id, ledger: Ledger) -> None:
    """Drop a held upload from the list. Its PDF stays stored, as every upload's does."""
    row = _held(tx, project_id, upload_id)
    ledger.append(
        tx,
        _event(
            project_id,
            UPLOAD_DISCARDED,
            actor_id,
            {"upload_id": str(upload_id), "file_name": row["file_name"]},
        ),
    )
    tx.execute("update retrieval_upload set status = 'discarded' where id = %s", (upload_id,))


__all__ = [
    "UPLOAD_ASSIGNED",
    "UPLOAD_DISCARDED",
    "UPLOAD_HELD",
    "UPLOAD_MATCHED",
    "HeldUpload",
    "UploadCandidate",
    "UploadPlan",
    "UploadResult",
    "assign_upload",
    "discard_upload",
    "held_uploads",
    "plan_upload",
    "upload_full_text",
]
