"""Search HTTP routes for the reviewer workbench."""

from fastapi import APIRouter, HTTPException

from kittum.api import Context  # noqa: TC001
from kittum.core import Id, Record, sha256_hex
from kittum.gateway import Gateway, HttpRequest, HttpResponse
from kittum.search import (
    CtgovSource,
    PubMedSource,
    SearchExecution,
    SearchPlan,
    Source,
    get_execution,
    import_file,
    list_executions,
    run,
)

router = APIRouter(prefix="/api/projects/{project_id}/search", tags=["search"])


class RunSearchRequest(Record):
    plan: SearchPlan


class ImportSearchRequest(Record):
    plan: SearchPlan
    data: str
    format: str


class _DefaultHttpxTransport:
    def send(self, request: HttpRequest) -> HttpResponse:
        import httpx2

        resp = httpx2.request(
            method=request.method,
            url=request.url,
            headers=dict(request.headers),
            content=request.body,
        )
        return HttpResponse(
            status=resp.status_code,
            headers=tuple(resp.headers.items()),
            body=resp.content,
            body_hash=sha256_hex(resp.content),
        )


class _DefaultGateway:
    def __init__(self) -> None:
        self._transport = _DefaultHttpxTransport()

    def fetch(self, source: str, request: HttpRequest) -> HttpResponse:
        _ = source
        return self._transport.send(request)


def _get_source(plan: SearchPlan, ctx: Context) -> Source:
    gw: Gateway = ctx.gateway or _DefaultGateway()
    if plan.source.lower() in ("pubmed", "medline"):
        return PubMedSource(
            gw,
            ctx.artifacts,
            contact_email=ctx.settings.contact_email,
            ncbi_api_key=ctx.settings.ncbi_api_key,
            clock=ctx.clock,
        )
    if plan.source.lower() in ("ctgov", "clinicaltrials"):
        return CtgovSource(
            gw,
            ctx.artifacts,
            clock=ctx.clock,
        )
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
    source = _get_source(body.plan, ctx)
    with ctx.db.transaction() as tx:
        return run(
            tx,
            project_id,
            body.plan,
            source,
            ledger=ctx.ledger,
            artifacts=ctx.artifacts,
            clock=ctx.clock,
        )


@router.post("/import", operation_id="importSearchFile")
def import_search(
    project_id: Id,
    body: ImportSearchRequest,
    ctx: Context,
) -> SearchExecution:
    with ctx.db.transaction() as tx:
        return import_file(
            tx,
            project_id,
            body.plan,
            body.data,
            body.format,
            ledger=ctx.ledger,
            artifacts=ctx.artifacts,
            clock=ctx.clock,
        )


@router.get("/executions", operation_id="listSearchExecutions")
def list_search_executions(
    project_id: Id,
    ctx: Context,
    topic_id: str | None = None,
) -> list[SearchExecution]:
    with ctx.db.transaction() as tx:
        return list_executions(tx, project_id, topic_id=topic_id)


@router.get("/executions/{execution_id}", operation_id="getSearchExecution")
def get_search_execution(
    project_id: Id,
    execution_id: Id,
    ctx: Context,
) -> SearchExecution:
    with ctx.db.transaction() as tx:
        exec_item = get_execution(tx, project_id, execution_id)
        if not exec_item:
            raise HTTPException(status_code=404, detail="Search execution not found")
        return exec_item
