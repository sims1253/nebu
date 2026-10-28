"""Open-access full texts for the reports sought for retrieval, as a job (DESIGN.md, "Full text").

    run = request_open_access(tx, project_id, actor_id=lead, ...)
    # `kittum worker` runs OPEN_ACCESS_JOB: the waterfall for each report, one at a time
    view = open_access_run(tx, project_id, run.id)

A report is sought for retrieval once it is included at title and abstract in some scope; a run
takes those that have no full text yet, in corpus order, when it is requested. For each one the
job collects the open-access locations enrichment found for its records, ranks them by the
waterfall (`kittum.enrich.full_text_candidates`: Europe PMC, Unpaywall, OpenAlex, then the
publisher's openly licensed PDF links from Crossref), and lets `kittum.documents.acquire` walk the
PDF locations and record every attempt. A PDF it gets is stored with its licence and becomes the
report's full text with its parsed layer (`attach_acquired_full_text`). A report that is uploaded
by hand meanwhile is left alone. Like the upload queue, the run is the methods lead's alone: it
names the reports included at title and abstract.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from psycopg.types.json import Jsonb

from kittum.app.lifecycle import LifecycleError, attach_acquired_full_text, full_text
from kittum.core import Clock, Id, NotFoundError, Record, new_id
from kittum.corpus import latest_revision, reports
from kittum.documents import AcquisitionAttempt, FullTextLocation, acquire
from kittum.enrich import OaLocation, RecordEnrichment, full_text_candidates
from kittum.gateway import default_policies
from kittum.ledger import EventDraft
from kittum.runs import JobOutput, JobSpec, PermanentJobError

if TYPE_CHECKING:
    from kittum.app.context import AppContext
    from kittum.db import Tx
    from kittum.gateway import Gateway
    from kittum.ledger import Ledger
    from kittum.runs import Handler, Job, JobContext, JobQueue
    from kittum.screening import Screening

OPEN_ACCESS_JOB = "retrieval.open_access"
OPEN_ACCESS_REQUESTED = "retrieval.open_access_requested"
NO_LOCATION = "retrieval.no_open_access_location"
OPEN_ACCESS_FINISHED = "retrieval.open_access_finished"

type ReportOutcome = Literal["acquired", "not_acquired", "no_location", "had_full_text"]


class AttemptView(Record):
    """One step of the waterfall for one report."""

    source: str
    url: str
    host: str
    status: int | None
    outcome: str
    licence: str | None
    error: str | None = None


class ReportRetrieval(Record):
    report_id: Id
    title: str
    outcome: ReportOutcome | None
    """None while the report waits its turn."""
    attempts: tuple[AttemptView, ...]
    source: str | None = None
    """Where the full text came from, when one was acquired."""
    licence: str | None = None
    document_id: Id | None = None
    page_count: int | None = None


class OpenAccessRun(Record):
    id: Id
    status: Literal["queued", "running", "done", "failed"]
    requested_by: Id
    requested_at: datetime
    finished_at: datetime | None
    reports: tuple[ReportRetrieval, ...]
    error: str | None = None


def _event(project_id: Id, type: str, actor_id: Id, payload: dict[str, Any]) -> EventDraft:
    return EventDraft(
        project_id=project_id, type=type, schema_version=1, actor_id=actor_id, payload=payload
    )


def sought_for_retrieval(tx: Tx, project_id: Id, screening: Screening) -> list[Id]:
    """The reports of the newest identity revision included at title and abstract in at least one
    scope, in corpus order."""
    revision = latest_revision(tx, project_id)
    if revision is None:
        raise LifecycleError("build the corpus first")
    included: set[str] = set()
    for info in screening.stages(tx, project_id):
        if info.stage != "title_abstract":
            continue
        found = screening.outcomes(
            tx, project_id, info.scope_id, info.stage, topic_id=info.topic_id
        )
        included |= {ref for ref, outcome in found.items() if outcome == "include"}
    return [
        r.report_id for r in reports(tx, project_id, revision.id) if str(r.report_id) in included
    ]


def request_open_access(
    tx: Tx,
    project_id: Id,
    screening: Screening,
    *,
    actor_id: Id,
    ledger: Ledger,
    queue: JobQueue,
    clock: Clock,
) -> OpenAccessRun:
    """Queue the waterfall for every report sought for retrieval that has no full text. An
    unfinished run is returned as it is."""
    active = tx.execute(
        "select r.id from retrieval_run r join job j on j.id = r.job_id"
        " where r.project_id = %s and r.finished_at is null and j.status in ('queued', 'running')"
        " order by r.requested_at desc limit 1",
        (project_id,),
    ).fetchone()
    if active is not None:
        return open_access_run(tx, project_id, active["id"])
    wanted = [
        r
        for r in sought_for_retrieval(tx, project_id, screening)
        if full_text(tx, project_id, str(r)) is None
    ]
    if not wanted:
        raise LifecycleError("every report sought for retrieval has its full text already")
    run_id = new_id()
    event = ledger.append(
        tx,
        _event(
            project_id,
            OPEN_ACCESS_REQUESTED,
            actor_id,
            {"run_id": str(run_id), "report_ids": [str(r) for r in wanted]},
        ),
    )
    job = queue.enqueue(
        tx,
        JobSpec(
            project_id=project_id,
            kind=OPEN_ACCESS_JOB,
            stage_key=f"{OPEN_ACCESS_JOB}:{run_id}",
            payload={"run_id": str(run_id)},
        ),
    )
    tx.execute(
        "insert into retrieval_run (id, project_id, requested_by, requested_at, job_id, event_id)"
        " values (%s, %s, %s, %s, %s, %s)",
        (run_id, project_id, actor_id, clock.now(), job.job_id, event.event_id),
    )
    with tx.cursor() as cur:
        cur.executemany(
            "insert into retrieval_run_report (run_id, report_id, ordinal) values (%s, %s, %s)",
            [(run_id, report_id, i) for i, report_id in enumerate(wanted)],
        )
    return open_access_run(tx, project_id, run_id)


def _attempt(raw: dict[str, Any]) -> AttemptView:
    attempt = AcquisitionAttempt.model_validate(raw)
    return AttemptView(
        source=attempt.source,
        url=attempt.url,
        host=urlsplit(attempt.url).hostname or attempt.url,
        status=attempt.status,
        outcome=attempt.outcome,
        licence=attempt.licence,
        error=attempt.error,
    )


def open_access_run(tx: Tx, project_id: Id, run_id: Id) -> OpenAccessRun:
    row = tx.execute(
        "select r.*, j.status as job_status, j.last_error from retrieval_run r"
        " join job j on j.id = r.job_id where r.project_id = %s and r.id = %s",
        (project_id, run_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"retrieval run {run_id} not found")
    titles = _titles(tx, project_id)
    items = tx.execute(
        "select rr.*, a.source, a.licence, a.document_id, d.page_count"
        " from retrieval_run_report rr"
        " left join acquisition a on a.id = rr.acquisition_id"
        " left join document d on d.id = a.document_id"
        " where rr.run_id = %s order by rr.ordinal",
        (run_id,),
    ).fetchall()
    if row["finished_at"] is not None:
        status, error = "done", None
    elif row["job_status"] in ("failed", "dead"):
        status, error = "failed", row["last_error"]
    else:
        status = "running" if row["job_status"] == "running" else "queued"
        error = row["last_error"]
    return OpenAccessRun(
        id=row["id"],
        status=status,
        requested_by=row["requested_by"],
        requested_at=row["requested_at"],
        finished_at=row["finished_at"],
        error=error,
        reports=tuple(
            ReportRetrieval(
                report_id=item["report_id"],
                title=titles.get(item["report_id"], ""),
                outcome=item["outcome"],
                attempts=tuple(_attempt(a) for a in item["attempts"]),
                source=item["source"],
                licence=item["licence"],
                document_id=item["document_id"],
                page_count=item["page_count"],
            )
            for item in items
        ),
    )


def latest_open_access_run(tx: Tx, project_id: Id) -> OpenAccessRun | None:
    row = tx.execute(
        "select id from retrieval_run where project_id = %s order by requested_at desc, id desc"
        " limit 1",
        (project_id,),
    ).fetchone()
    return open_access_run(tx, project_id, row["id"]) if row else None


def report_attempts(tx: Tx, project_id: Id, report_id: Id) -> list[AttemptView]:
    """Every waterfall attempt for a report, over all runs, oldest first."""
    rows = tx.execute(
        "select rr.attempts from retrieval_run_report rr join retrieval_run r on r.id = rr.run_id"
        " where r.project_id = %s and rr.report_id = %s order by r.requested_at, r.id",
        (project_id, report_id),
    ).fetchall()
    return [_attempt(a) for r in rows for a in r["attempts"]]


def _titles(tx: Tx, project_id: Id) -> dict[Id, str]:
    revision = latest_revision(tx, project_id)
    if revision is None:
        return {}
    return {
        r.report_id: max(r.records, key=lambda x: len(x.abstract or "")).title
        for r in reports(tx, project_id, revision.id)
    }


# The job


def locations(tx: Tx, project_id: Id, report_id: Id) -> list[FullTextLocation]:
    """The PDF locations enrichment found for a report's records, in waterfall order, each with the
    terms of the source it came from."""
    revision = latest_revision(tx, project_id)
    if revision is None:
        return []
    occurrences = [
        record.occurrence_id
        for report in reports(tx, project_id, revision.id)
        if report.report_id == report_id
        for record in report.records
    ]
    rows = tx.execute(
        "select oa_locations from record_enrichment where project_id = %s"
        " and occurrence_id = any(%s)",
        (project_id, occurrences),
    ).fetchall()
    merged = RecordEnrichment(
        occurrence_id=report_id,
        provider_record_id=str(report_id),
        oa_locations=tuple(OaLocation.model_validate(o) for r in rows for o in r["oa_locations"]),
    )
    terms = {source: policy.terms_url for source, policy in default_policies().items()}
    seen: set[str] = set()
    found = []
    for candidate in full_text_candidates(merged):
        if candidate.format != "pdf" or candidate.url in seen:
            continue
        seen.add(candidate.url)
        found.append(
            FullTextLocation(
                source=candidate.source,
                url=candidate.url,
                licence=candidate.licence,
                terms_url=terms.get(candidate.source),
            )
        )
    return found


def _retrieve(
    tx: Tx,
    run: dict[str, Any],
    report_id: Id,
    *,
    ctx: AppContext,
    gateway: Gateway,
    ledger: Ledger,
) -> None:
    project_id, actor = run["project_id"], run["requested_by"]
    ref = str(report_id)
    outcome: ReportOutcome
    attempts: tuple[AcquisitionAttempt, ...] = ()
    acquisition_id = None
    event_id = None
    if full_text(tx, project_id, ref) is not None:
        outcome = "had_full_text"  # uploaded by hand since the run was requested
    elif not (found := locations(tx, project_id, report_id)):
        outcome = "no_location"
        event_id = ledger.append(
            tx, _event(project_id, NO_LOCATION, actor, {"run_id": str(run["id"]), "item_ref": ref})
        ).event_id
    else:
        result = acquire(
            tx,
            gateway=gateway,
            artifacts=ctx.artifacts,
            ledger=ledger,
            clock=ctx.clock,
            project_id=project_id,
            actor_id=actor,
            locations=found,
        )
        attempts = result.attempts
        if result.acquisition is None:
            outcome = "not_acquired"
        else:
            outcome = "acquired"
            acquisition_id = result.acquisition.id
            attach_acquired_full_text(
                tx,
                project_id,
                ref,
                result.acquisition,
                actor_id=actor,
                artifacts=ctx.artifacts,
                ledger=ledger,
            )
    tx.execute(
        "update retrieval_run_report set outcome = %s, attempts = %s, acquisition_id = %s,"
        " event_id = %s, finished_at = %s where run_id = %s and report_id = %s",
        (
            outcome,
            Jsonb([a.model_dump(mode="json") for a in attempts]),
            acquisition_id,
            event_id,
            ctx.clock.now(),
            run["id"],
            report_id,
        ),
    )


def open_access_handler(ctx: AppContext, gateway: Gateway, ledger: Ledger) -> Handler:
    def handle(job: Job, job_ctx: JobContext) -> JobOutput:
        run_id = UUID(job.spec.payload["run_id"])
        with ctx.db.transaction() as tx:
            run = tx.execute("select * from retrieval_run where id = %s", (run_id,)).fetchone()
            if run is None:
                raise PermanentJobError(f"retrieval run {run_id} not found")
            pending = [
                r["report_id"]
                for r in tx.execute(
                    "select report_id from retrieval_run_report where run_id = %s"
                    " and outcome is null order by ordinal",
                    (run_id,),
                ).fetchall()
            ]
        for report_id in pending:
            if job_ctx.lease_lost.is_set():
                return JobOutput()  # another worker resumes after the last report committed
            with ctx.db.transaction() as tx:
                _retrieve(tx, run, report_id, ctx=ctx, gateway=gateway, ledger=ledger)

        def publish(tx: Tx) -> None:
            view = open_access_run(tx, run["project_id"], run_id)
            counts: dict[str, int] = {}
            for item in view.reports:
                counts[item.outcome or "pending"] = counts.get(item.outcome or "pending", 0) + 1
            ledger.append(
                tx,
                _event(
                    run["project_id"],
                    OPEN_ACCESS_FINISHED,
                    run["requested_by"],
                    {"run_id": str(run_id), "outcomes": counts},
                ),
            )
            tx.execute(
                "update retrieval_run set finished_at = %s where id = %s",
                (ctx.clock.now(), run_id),
            )

        return JobOutput(publish=publish)

    return handle


__all__ = [
    "NO_LOCATION",
    "OPEN_ACCESS_FINISHED",
    "OPEN_ACCESS_JOB",
    "OPEN_ACCESS_REQUESTED",
    "AttemptView",
    "OpenAccessRun",
    "ReportRetrieval",
    "latest_open_access_run",
    "locations",
    "open_access_handler",
    "open_access_run",
    "report_attempts",
    "request_open_access",
    "sought_for_retrieval",
]
