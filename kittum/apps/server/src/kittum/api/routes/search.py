"""Search HTTP routes for the reviewer workbench."""

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException

from kittum.api import Context  # noqa: TC001
from kittum.api.routes.projects import Viewer  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.app import NotLead, require_lead
from kittum.core import Id, Record
from kittum.ledger import Ledger  # noqa: TC001
from kittum.search import (
    CtgovSource,
    PubMedSource,
    SearchExecution,
    SearchPlan,
    Source,
    SourceError,
    get_execution,
    import_file,
    list_executions,
    run,
)

if TYPE_CHECKING:
    from kittum.db import Tx

router = APIRouter(prefix="/api/projects/{project_id}/search", tags=["search"])


class RunSearchRequest(Record):
    plan: SearchPlan
    actor_id: Id


class ImportSearchRequest(Record):
    plan: SearchPlan
    data: str
    format: str
    actor_id: Id


def _require_lead(tx: Tx, project_id: Id, viewer: Id) -> None:
    try:
        require_lead(tx, project_id, viewer)
    except NotLead as e:
        raise HTTPException(status_code=403, detail=str(e)) from e


def _ledger(ctx: Context) -> Ledger:
    if ctx.ledger is None:
        raise HTTPException(status_code=503, detail="No ledger is configured")
    return ctx.ledger


def _get_source(plan: SearchPlan, ctx: Context) -> Source:
    if ctx.gateway is None:
        raise HTTPException(status_code=503, detail="No HTTP gateway is configured")
    if plan.source.lower() in ("pubmed", "medline"):
        return PubMedSource(
            ctx.gateway,
            ctx.artifacts,
            contact_email=ctx.settings.contact_email,
            ncbi_api_key=ctx.settings.ncbi_api_key,
            clock=ctx.clock,
        )
    if plan.source.lower() in ("ctgov", "clinicaltrials"):
        return CtgovSource(ctx.gateway, ctx.artifacts, clock=ctx.clock)
    raise HTTPException(
        status_code=400,
        detail=f"Unsupported live search source '{plan.source}'. Use file import instead.",
    )


@router.post("/run", operation_id="runSearch")
def run_search(
    project_id: Id,
    body: RunSearchRequest,
    ctx: Context,
) -> SearchExecution:
    ledger = _ledger(ctx)
    source = _get_source(body.plan, ctx)
    try:
        with ctx.db.transaction() as tx:
            return run(
                tx,
                project_id,
                body.plan,
                source,
                ledger=ledger,
                actor_id=body.actor_id,
                artifacts=ctx.artifacts,
            )
    except SourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/import", operation_id="importSearchFile")
def import_search(
    project_id: Id,
    body: ImportSearchRequest,
    ctx: Context,
) -> SearchExecution:
    ledger = _ledger(ctx)
    try:
        with ctx.db.transaction() as tx:
            return import_file(
                tx,
                project_id,
                body.plan,
                body.data,
                body.format,
                ledger=ledger,
                actor_id=body.actor_id,
                artifacts=ctx.artifacts,
                clock=ctx.clock,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/executions", operation_id="listSearchExecutions")
def list_search_executions(
    project_id: Id,
    viewer: Viewer,
    ctx: Context,
    topic_id: str | None = None,
) -> list[SearchExecution]:
    """Every execution with its records, for the project's methods lead only (403 otherwise):
    a reviewer who could list every record could find the sentinels in their queue."""
    with ctx.db.transaction() as tx:
        _require_lead(tx, project_id, viewer)
        return list_executions(tx, project_id, topic_id=topic_id)


@router.get("/executions/{execution_id}", operation_id="getSearchExecution")
def get_search_execution(
    project_id: Id,
    execution_id: Id,
    viewer: Viewer,
    ctx: Context,
) -> SearchExecution:
    with ctx.db.transaction() as tx:
        _require_lead(tx, project_id, viewer)
        exec_item = get_execution(tx, project_id, execution_id)
        if not exec_item:
            raise HTTPException(status_code=404, detail="Search execution not found")
        return exec_item
