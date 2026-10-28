"""Extraction routes for the workbench: read a schema, run a layer, list runs, review their
fields, accept values.

The methods lead requests runs, on reports included at full text only. Every read names its
caller in `X-Kittum-Reviewer` (a stand-in until the API has authentication) and shows a run only
to someone who may see it (`kittum.app.visible_reports`): a run exists only for an included
report, so a screener who has not decided the report yet must not learn of it.

A run is a job: `POST .../runs` answers 202 with the queued run, and a worker running
`kittum.extraction.run_handler` fills it. Accepting needs the time the source was on screen;
an edit needs the value's span as code-point offsets into the layer text, and its quote.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException
from pydantic import Field

from kittum.api import AppContext, Context  # noqa: TC001  FastAPI resolves them at runtime
from kittum.api.routes.projects import Viewer  # noqa: TC001  FastAPI resolves it at runtime
from kittum.app import (
    LifecycleError,
    NotLead,
    check_extractable,
    require_lead,
    visible_reports,
)
from kittum.core import Id, NotFoundError, Record
from kittum.extraction import (
    Acceptance,
    AcceptRequest,
    AgentConfig,
    Assertion,
    Edit,
    ExtractionReview,
    ExtractionRun,
    ExtractionSchema,
    GroupAcceptRequest,
    Path,
    ReportInfo,
    ReviewField,
    RunListing,
    RunRequest,
    SchemaError,
    accept,
    assertions,
    get_run,
    group_accept,
    list_fields,
    list_runs,
    load_schema,
    request_run,
    review,
    run_of_field,
)
from kittum.runs import CommandIdReused, CommandRejected, Commands

if TYPE_CHECKING:
    from collections.abc import Generator

    from kittum.db import Tx

router = APIRouter(prefix="/api/projects/{project_id}/extraction", tags=["extraction"])


class SchemaText(Record):
    text: str
    """An extraction schema as YAML or JSON."""


class RunBody(Record):
    command_id: Id
    actor_id: Id
    layer_id: Id
    study_id: Id
    report_id: Id
    extraction_schema: ExtractionSchema
    agent: AgentConfig | None = None  # None: the Janus path only
    report: ReportInfo = ReportInfo()


class AcceptBody(Record):
    command_id: Id
    reviewer_id: Id
    path: Path
    source_viewed_ms: Annotated[int, Field(ge=0)]
    override_failed_checks: bool = False


class EditBody(Record):
    command_id: Id
    reviewer_id: Id
    path: Path  # the proposal the reviewer corrected
    source_viewed_ms: Annotated[int, Field(ge=0)]
    value: str | None
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    quote: str


class GroupAcceptBody(Record):
    command_id: Id
    reviewer_id: Id
    field_ids: Annotated[tuple[Id, ...], Field(min_length=1)]
    list_viewed_ms: Annotated[int, Field(ge=0)]


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except NotLead as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except (CommandRejected, CommandIdReused, LifecycleError) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _visible_run(tx: Tx, project_id: Id, run_id: Id, viewer: Id) -> ExtractionRun:
    """The run, or not found for anyone who may not see it: its existence is the news."""
    run = get_run(tx, project_id, run_id)
    if run.report_id not in visible_reports(tx, project_id, viewer, [run.report_id]):
        raise NotFoundError(f"extraction run {run_id}")
    return run


def _commands(ctx: AppContext) -> Commands:
    if ctx.ledger is None or ctx.queue is None or ctx.receipts is None:
        raise HTTPException(status_code=503, detail="no ledger or job queue is configured")
    return Commands(ctx.ledger, ctx.queue, ctx.receipts, ctx.clock)


@router.post("/schemas/parse", operation_id="parseExtractionSchema")
def parse_schema_route(project_id: Id, body: SchemaText) -> ExtractionSchema:  # noqa: ARG001
    """Read and check a schema for `requestExtractionRun`; 422 with the problem."""
    try:
        return load_schema(body.text)
    except SchemaError as e:
        # FastAPI's validation-error shape, so clients read one 422 format.
        detail = [{"loc": ["body", "text"], "msg": str(e), "type": "invalid_schema"}]
        raise HTTPException(status_code=422, detail=detail) from e


@router.get("/models", operation_id="listExtractionModels")
def list_models_route(project_id: Id, ctx: Context) -> list[str]:  # noqa: ARG001
    """The model recipes with a configured server, for the LLM path; empty means Janus only."""
    return sorted(ctx.backends)


@router.get("/runs", operation_id="listExtractionRuns")
def list_runs_route(project_id: Id, viewer: Viewer, ctx: Context) -> list[RunListing]:
    """The project's runs the viewer may see, newest first, with their report and review counts."""
    with ctx.db.transaction() as tx:
        runs = list_runs(tx, project_id)
        shown = visible_reports(tx, project_id, viewer, [r.run.report_id for r in runs])
        return [r for r in runs if r.run.report_id in shown]


@router.post("/runs", operation_id="requestExtractionRun", status_code=202)
def request_run_route(project_id: Id, body: RunBody, ctx: Context) -> ExtractionRun:
    """Queue both paths over a layer of an included report's full text, for the methods lead
    (403 otherwise; 409 unless the report is included at full text and the layer is its full
    text's). Retrying with the same `command_id` returns the same run."""
    commands = _commands(ctx)
    request = RunRequest(project_id=project_id, **body.model_dump())
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        check_extractable(
            tx, project_id, body.report_id, body.layer_id, ctx.screening(), ctx.artifacts
        )
        return request_run(tx, commands, request, clock=ctx.clock)


@router.get("/runs/{run_id}", operation_id="getExtractionRun")
def get_run_route(project_id: Id, run_id: Id, viewer: Viewer, ctx: Context) -> ExtractionRun:
    with _errors(), ctx.db.transaction() as tx:
        return _visible_run(tx, project_id, run_id, viewer)


@router.get("/runs/{run_id}/fields", operation_id="listExtractionFields")
def list_fields_route(
    project_id: Id, run_id: Id, viewer: Viewer, ctx: Context
) -> list[ReviewField]:
    """One entry per semantic key, in schema order, with both paths' proposals."""
    with _errors(), ctx.db.transaction() as tx:
        _visible_run(tx, project_id, run_id, viewer)
        return list_fields(tx, project_id, run_id)


@router.get("/runs/{run_id}/review", operation_id="getExtractionReview")
def get_review_route(project_id: Id, run_id: Id, viewer: Viewer, ctx: Context) -> ExtractionReview:
    """The extraction screen: the report, its layer, every anchor, and the fields."""
    with _errors(), ctx.db.transaction() as tx:
        _visible_run(tx, project_id, run_id, viewer)
        return review(tx, project_id, run_id, artifacts=ctx.artifacts)


@router.post("/fields/{field_id}/accept", operation_id="acceptExtractionField")
def accept_route(project_id: Id, field_id: Id, body: AcceptBody, ctx: Context) -> Acceptance:
    """Accept one path's value as it stands. 409 with the reason if it cannot be accepted."""
    commands = _commands(ctx)
    request = AcceptRequest(project_id=project_id, field_id=field_id, **body.model_dump())
    with _errors(), ctx.db.transaction() as tx:
        run = run_of_field(tx, project_id, field_id)
        _visible_run(tx, project_id, run.id, body.reviewer_id)
        return accept(tx, commands, request, artifacts=ctx.artifacts, clock=ctx.clock)


@router.post("/fields/{field_id}/edit", operation_id="editAndAcceptExtractionField")
def edit_route(project_id: Id, field_id: Id, body: EditBody, ctx: Context) -> Acceptance:
    """Accept the reviewer's own value with the span it came from. The span must hold it."""
    commands = _commands(ctx)
    request = AcceptRequest(
        command_id=body.command_id,
        project_id=project_id,
        field_id=field_id,
        reviewer_id=body.reviewer_id,
        path=body.path,
        source_viewed_ms=body.source_viewed_ms,
        edit=Edit(value=body.value, start=body.start, end=body.end, quote=body.quote),
    )
    with _errors(), ctx.db.transaction() as tx:
        run = run_of_field(tx, project_id, field_id)
        _visible_run(tx, project_id, run.id, body.reviewer_id)
        return accept(tx, commands, request, artifacts=ctx.artifacts, clock=ctx.clock)


@router.post("/runs/{run_id}/group-accept", operation_id="groupAcceptExtractionFields")
def group_accept_route(
    project_id: Id, run_id: Id, body: GroupAcceptBody, ctx: Context
) -> list[Acceptance]:
    """Accept agreeing, validated standard fields together; 409 if any field may not be."""
    commands = _commands(ctx)
    request = GroupAcceptRequest(project_id=project_id, run_id=run_id, **body.model_dump())
    with _errors(), ctx.db.transaction() as tx:
        _visible_run(tx, project_id, run_id, body.reviewer_id)
        return group_accept(tx, commands, request, clock=ctx.clock)


@router.get("/assertions", operation_id="listExtractionAssertions")
def assertions_route(project_id: Id, viewer: Viewer, ctx: Context) -> list[Assertion]:
    """The project's accepted values the viewer may see, the latest per semantic key."""
    with ctx.db.transaction() as tx:
        found = assertions(tx, project_id)
        shown = visible_reports(tx, project_id, viewer, list({a.key.report_id for a in found}))
        return [a for a in found if a.key.report_id in shown]
