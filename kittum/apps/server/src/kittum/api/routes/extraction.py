"""Extraction routes for the workbench: run a layer, review its fields, accept values.

A run is a job: `POST .../runs` answers 202 with the queued run, and a worker running
`kittum.extraction.run_handler` fills it. Accepting needs the time the source was on screen;
an edit needs the value's span as code-point offsets into the layer text, and its quote.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException
from pydantic import Field

from kittum.api import AppContext, Context  # noqa: TC001  FastAPI resolves them at runtime
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
    RunRequest,
    accept,
    assertions,
    get_run,
    group_accept,
    list_fields,
    request_run,
    review,
)
from kittum.runs import CommandIdReused, CommandRejected, Commands

if TYPE_CHECKING:
    from collections.abc import Generator

router = APIRouter(prefix="/api/projects/{project_id}/extraction", tags=["extraction"])


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
    except (CommandRejected, CommandIdReused) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _commands(ctx: AppContext) -> Commands:
    if ctx.ledger is None or ctx.queue is None or ctx.receipts is None:
        raise HTTPException(status_code=503, detail="no ledger or job queue is configured")
    return Commands(ctx.ledger, ctx.queue, ctx.receipts, ctx.clock)


@router.post("/runs", operation_id="requestExtractionRun", status_code=202)
def request_run_route(project_id: Id, body: RunBody, ctx: Context) -> ExtractionRun:
    """Queue both paths over a layer. Retrying with the same `command_id` returns the same run."""
    commands = _commands(ctx)
    request = RunRequest(project_id=project_id, **body.model_dump())
    with _errors(), ctx.db.transaction() as tx:
        return request_run(tx, commands, request, clock=ctx.clock)


@router.get("/runs/{run_id}", operation_id="getExtractionRun")
def get_run_route(project_id: Id, run_id: Id, ctx: Context) -> ExtractionRun:
    with _errors(), ctx.db.transaction() as tx:
        return get_run(tx, project_id, run_id)


@router.get("/runs/{run_id}/fields", operation_id="listExtractionFields")
def list_fields_route(project_id: Id, run_id: Id, ctx: Context) -> list[ReviewField]:
    """One entry per semantic key, in schema order, with both paths' proposals."""
    with _errors(), ctx.db.transaction() as tx:
        get_run(tx, project_id, run_id)
        return list_fields(tx, project_id, run_id)


@router.get("/runs/{run_id}/review", operation_id="getExtractionReview")
def get_review_route(project_id: Id, run_id: Id, ctx: Context) -> ExtractionReview:
    """The extraction screen: the report, its layer, every anchor, and the fields."""
    with _errors(), ctx.db.transaction() as tx:
        return review(tx, project_id, run_id, artifacts=ctx.artifacts)


@router.post("/fields/{field_id}/accept", operation_id="acceptExtractionField")
def accept_route(project_id: Id, field_id: Id, body: AcceptBody, ctx: Context) -> Acceptance:
    """Accept one path's value as it stands. 409 with the reason if it cannot be accepted."""
    commands = _commands(ctx)
    request = AcceptRequest(project_id=project_id, field_id=field_id, **body.model_dump())
    with _errors(), ctx.db.transaction() as tx:
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
        return accept(tx, commands, request, artifacts=ctx.artifacts, clock=ctx.clock)


@router.post("/runs/{run_id}/group-accept", operation_id="groupAcceptExtractionFields")
def group_accept_route(
    project_id: Id, run_id: Id, body: GroupAcceptBody, ctx: Context
) -> list[Acceptance]:
    """Accept agreeing, validated standard fields together; 409 if any field may not be."""
    commands = _commands(ctx)
    request = GroupAcceptRequest(project_id=project_id, run_id=run_id, **body.model_dump())
    with _errors(), ctx.db.transaction() as tx:
        return group_accept(tx, commands, request, clock=ctx.clock)


@router.get("/assertions", operation_id="listExtractionAssertions")
def assertions_route(project_id: Id, ctx: Context) -> list[Assertion]:
    """The project's accepted values, the latest acceptance per semantic key."""
    with ctx.db.transaction() as tx:
        return assertions(tx, project_id)
