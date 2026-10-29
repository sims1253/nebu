"""Enrichment and open-access retrieval routes: start a run, follow its progress, read what it
found for one record or one report, retry the sources that failed, export the reports still
without a full text, and upload many PDFs at once.

Every route is the methods lead's alone (`require_lead`, 403 otherwise), as the corpus catalogue
is: enrichment lists every record, and an open-access run names the reports included at title and
abstract. A run is a job: the start routes answer 202 with the queued run, and a worker running
`kittum worker` fills it; the workbench polls the run until it is done.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from starlette.concurrency import run_in_threadpool

from kittum.api import AppContext, Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.api.routes.projects import Viewer, lead_pdf
from kittum.app import (
    AttemptView,
    EnrichmentRun,
    FullText,
    HeldUpload,
    LifecycleError,
    MissingReport,
    NotLead,
    OpenAccessRun,
    RecordEnrichmentView,
    RecordFlag,
    RetryPlan,
    RetryPreview,
    UploadResult,
    assign_upload,
    discard_upload,
    held_uploads,
    latest_enrichment_run,
    latest_open_access_run,
    missing_csv,
    missing_full_texts,
    missing_ris,
    plan_upload,
    record_enrichment,
    record_flags,
    report_attempts,
    request_enrichment,
    request_open_access,
    request_retry,
    require_lead,
    retry_preview,
    upload_full_text,
)
from kittum.core import Id, NotFoundError, Record
from kittum.documents import NotAPdf, first_pages

if TYPE_CHECKING:
    from collections.abc import Generator

    from kittum.runs import JobQueue

router = APIRouter(prefix="/api/projects/{project_id}", tags=["enrichment"])


class StartRequest(Record):
    actor_id: Id


class AssignRequest(Record):
    actor_id: Id
    report_id: Id


_PDF_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
    }
}
_EXPORT = {"csv": ("text/csv", "csv"), "ris": ("application/x-research-info-systems", "ris")}


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


@router.get("/retrieval/retry", operation_id="previewRetry")
def preview_retry(project_id: Id, viewer: Viewer, ctx: Context) -> RetryPreview:
    """What a retry would ask again: the records a source failed for in their last enrichment,
    and the reports without a full text a source failed for in their last retrieval."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        return retry_preview(tx, project_id, ctx.screening())


@router.post("/retrieval/retry", operation_id="retryFailedSources", status_code=202)
def retry(project_id: Id, body: StartRequest, ctx: Context) -> RetryPlan:
    """Ask the failed sources again: enrich the records they failed for, then retrieve the reports
    among them still without a full text; and retrieve again the reports a source failed for.
    409 while a run is going or when no source failed."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        return request_retry(
            tx,
            project_id,
            ctx.screening(),
            actor_id=body.actor_id,
            ledger=ctx.require_ledger(),
            queue=_queue(ctx),
            clock=ctx.clock,
        )


@router.get("/retrieval/missing", operation_id="listMissingFullTexts")
def list_missing(project_id: Id, viewer: Viewer, ctx: Context) -> list[MissingReport]:
    """The reports sought for retrieval without a usable full text, with every attempt for each."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        return missing_full_texts(tx, project_id, ctx.screening())


@router.get(
    "/retrieval/missing/export",
    operation_id="exportMissingFullTexts",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def export_missing(
    project_id: Id,
    viewer: Viewer,
    ctx: Context,
    format: Annotated[Literal["csv", "ris"], Query()] = "csv",
) -> Response:
    """The same list as a CSV or RIS file, to hand to whoever can send the PDFs."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        missing = missing_full_texts(tx, project_id, ctx.screening())
        content = missing_csv(missing) if format == "csv" else missing_ris(tx, project_id, missing)
    media_type, extension = _EXPORT[format]
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="missing-full-texts.{extension}"'},
    )


def _upload(
    ctx: AppContext,
    project_id: Id,
    file_name: str,
    pdf: bytes,
    actor_id: Id,
    licence: str | None,
) -> UploadResult:
    # Match first, reading only; then write in a transaction of its own, which checks that the
    # matched report still has no full text.
    pages = first_pages(pdf)
    with ctx.db.transaction() as tx:
        require_lead(tx, project_id, actor_id)
        plan = plan_upload(tx, project_id, file_name, pages, screening=ctx.screening())
    with ctx.db.transaction() as tx:
        return upload_full_text(
            tx,
            project_id,
            file_name,
            pdf,
            actor_id=actor_id,
            screening=ctx.screening(),
            artifacts=ctx.artifacts,
            ledger=ctx.require_ledger(),
            clock=ctx.clock,
            licence=licence,
            plan=plan,
        )


@router.post(
    "/retrieval/uploads",
    operation_id="uploadFullTexts",
    status_code=201,
    openapi_extra=_PDF_BODY,
)
async def upload(
    project_id: Id,
    request: Request,
    ctx: Context,
    actor_id: Annotated[Id, Query()],
    file_name: Annotated[str, Query()],
    licence: Annotated[str | None, Query()] = None,
) -> UploadResult:
    """One PDF of a bulk upload: attached to the report its file name or first pages match, or
    held for the lead when it matches none or several. 415 for a file that is not a PDF, 413 for
    one larger than `KITTUM_UPLOAD_MAX_BYTES`."""
    pdf = await lead_pdf(request, ctx, project_id, actor_id)
    try:
        with _errors():
            return await run_in_threadpool(
                _upload, ctx, project_id, file_name, pdf, actor_id, licence
            )
    except NotAPdf as e:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"not a PDF: {e}") from e


@router.get("/retrieval/uploads", operation_id="listHeldUploads")
def list_held(project_id: Id, viewer: Viewer, ctx: Context) -> list[HeldUpload]:
    """The uploads waiting for the lead to assign or discard them, oldest first."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        return held_uploads(tx, project_id)


@router.post("/retrieval/uploads/{upload_id}/assign", operation_id="assignHeldUpload")
def assign(project_id: Id, upload_id: Id, body: AssignRequest, ctx: Context) -> FullText:
    """Make a held upload the full text of the report the lead chose."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        return assign_upload(
            tx,
            project_id,
            upload_id,
            body.report_id,
            actor_id=body.actor_id,
            artifacts=ctx.artifacts,
            ledger=ctx.require_ledger(),
        )


@router.post(
    "/retrieval/uploads/{upload_id}/discard", operation_id="discardHeldUpload", status_code=204
)
def discard(project_id: Id, upload_id: Id, body: StartRequest, ctx: Context) -> None:
    """Take a held upload off the list."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        discard_upload(
            tx, project_id, upload_id, actor_id=body.actor_id, ledger=ctx.require_ledger()
        )
