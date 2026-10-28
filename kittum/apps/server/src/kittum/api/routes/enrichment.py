"""Enrichment and open-access retrieval routes: start a run, follow its progress, and read what
it found for one record or one report.

Every route is the methods lead's alone (`require_lead`, 403 otherwise), as the corpus catalogue
is: enrichment lists every record, and an open-access run names the reports included at title and
abstract. A run is a job: the start routes answer 202 with the queued run, and a worker running
`kittum worker` fills it; the workbench polls the run until it is done.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, status

from kittum.api import AppContext, Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.api.routes.projects import Viewer  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.app import (
    AttemptView,
    EnrichmentRun,
    LifecycleError,
    NotLead,
    OpenAccessRun,
    RecordEnrichmentView,
    RecordFlag,
    latest_enrichment_run,
    latest_open_access_run,
    record_enrichment,
    record_flags,
    report_attempts,
    request_enrichment,
    request_open_access,
    require_lead,
)
from kittum.core import Id, NotFoundError, Record

if TYPE_CHECKING:
    from collections.abc import Generator

    from kittum.runs import JobQueue

router = APIRouter(prefix="/api/projects/{project_id}", tags=["enrichment"])


class StartRequest(Record):
    actor_id: Id


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except NotLead as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e


def _queue(ctx: AppContext) -> JobQueue:
    if ctx.queue is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no job queue is configured")
    return ctx.queue


@router.post(
    "/enrichment/runs", operation_id="startEnrichment", status_code=status.HTTP_202_ACCEPTED
)
def start_enrichment(project_id: Id, body: StartRequest, ctx: Context) -> EnrichmentRun:
    """Enrich every record of the newest identity revision from the open sources. A run that is
    still going is returned instead of a second one."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        return request_enrichment(
            tx,
            project_id,
            actor_id=body.actor_id,
            trigger="requested",
            ledger=ctx.require_ledger(),
            queue=_queue(ctx),
            clock=ctx.clock,
        )


@router.get("/enrichment/runs/latest", operation_id="getLatestEnrichmentRun")
def latest_enrichment(project_id: Id, viewer: Viewer, ctx: Context) -> EnrichmentRun:
    """The newest run with its progress per source; 404 before the first."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        found = latest_enrichment_run(tx, project_id)
        if found is None:
            raise NotFoundError("the corpus has not been enriched yet")
        return found


@router.get("/enrichment/records", operation_id="listRecordEnrichmentFlags")
def list_flags(project_id: Id, viewer: Viewer, ctx: Context) -> list[RecordFlag]:
    """For every enriched record: retraction and other notices, the sources that answered, and
    whether an open-access file is known."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        return record_flags(tx, project_id)


@router.get("/enrichment/records/{occurrence_id}", operation_id="getRecordEnrichment")
def get_record(
    project_id: Id, occurrence_id: Id, viewer: Viewer, ctx: Context
) -> RecordEnrichmentView:
    """One record as enrichment completed it: where each field came from, its identifiers,
    retraction and correction notices, open-access locations, and entity tags."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        return record_enrichment(tx, project_id, occurrence_id)


@router.post(
    "/retrieval/open-access",
    operation_id="startOpenAccessRetrieval",
    status_code=status.HTTP_202_ACCEPTED,
)
def start_open_access(project_id: Id, body: StartRequest, ctx: Context) -> OpenAccessRun:
    """Walk the open-access waterfall for every report sought for retrieval without a full text."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        return request_open_access(
            tx,
            project_id,
            ctx.screening(),
            actor_id=body.actor_id,
            ledger=ctx.require_ledger(),
            queue=_queue(ctx),
            clock=ctx.clock,
        )


@router.get("/retrieval/open-access/latest", operation_id="getLatestOpenAccessRetrieval")
def latest_open_access(project_id: Id, viewer: Viewer, ctx: Context) -> OpenAccessRun:
    """The newest open-access run, each report with its attempts; 404 before the first."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        found = latest_open_access_run(tx, project_id)
        if found is None:
            raise NotFoundError("no open-access retrieval has run yet")
        return found


@router.get("/retrieval/reports/{report_id}/attempts", operation_id="listReportAcquisitionAttempts")
def list_attempts(project_id: Id, report_id: Id, viewer: Viewer, ctx: Context) -> list[AttemptView]:
    """Every waterfall attempt for one report, over all runs, oldest first."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        return report_attempts(tx, project_id, report_id)
