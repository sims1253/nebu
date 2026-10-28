"""Screening routes: the reviewer's queue, decide, undo, adjudication, and status.

Until the API has authentication (WP-15), the caller names themselves in the `X-Kittum-Reviewer`
header with their actor ID. The service enforces blinding against that ID: a reviewer gets only
their own assignments, no AI output before their own provisional decision, no sentinel or audit
flags, and only their own counts. The header is a stand-in, not a security boundary; anyone who
can reach the API can claim any actor ID.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Header, HTTPException, Query, status

from kittum.api import AppContext, Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.app import (
    AssignmentFullText,
    LifecycleError,
    NotLead,
    full_text,
    mentions,
    open_screening,
    parse_sentinels,
    require_lead,
)
from kittum.core import Id, NotFoundError, Record
from kittum.documents import get_layer
from kittum.protocol import Stage
from kittum.runs import CommandIdReused
from kittum.screening import (
    AdjudicationItem,
    AdjudicationResult,
    DecideResult,
    Decision,
    DecisionTiming,
    InvalidDecision,
    NotAssigned,
    Phase,
    QueueItem,
    RevealSetting,
    ScopeStatus,
    Screening,
    Sentinel,
    StageInfo,
    StaleRevision,
    UndoResult,
)

if TYPE_CHECKING:
    from collections.abc import Generator

router = APIRouter(prefix="/api/projects/{project_id}/screening", tags=["screening"])

Reviewer = Annotated[
    Id,
    Header(
        alias="X-Kittum-Reviewer",
        description="The caller's actor ID. A stand-in until the API has authentication.",
    ),
]
TopicId = Annotated[
    str | None, Query(description="Needed only when the scope is screened in several topics.")
]


class DecideRequest(Record):
    command_id: Id
    """Chosen by the client; a retry with the same ID returns the first result."""
    decision: Decision
    phase: Phase
    expected_etag: str
    """The `etag` the client last saw; a stale one is rejected with 409."""
    timing: DecisionTiming


class UndoRequest(Record):
    command_id: Id
    expected_etag: str
    """The `etag` the client last saw; a stale one is rejected with 409."""


class AdjudicateRequest(Record):
    command_id: Id
    expected_etag: str
    """The `etag` the client last saw; a stale one is rejected with 409."""
    adopted: str | None = None
    """A column label ("A", "B", ...) to adopt, or None with an own `decision`."""
    decision: Decision | None = None
    timing: DecisionTiming | None = None


class OpenStageRequest(Record):
    scope_id: str
    stage: Stage
    actor_id: Id
    """The methods lead who opens the stage."""
    reveal: RevealSetting = "on_disagreement"
    topic_id: str | None = None
    sentinels: tuple[Sentinel, ...] = ()
    """Records with known labels for the queues; a full-text stage without any draws them from
    the project's sentinel full texts."""


class SentinelFile(Record):
    text: str
    """A YAML or JSON list of sentinels, as `kittum project open-stage --sentinels` reads."""


def _screening(ctx: AppContext) -> Screening:
    if ctx.ledger is None:
        raise HTTPException(status_code=503, detail="no ledger is configured")
    return Screening.postgres(ctx.ledger, ctx.clock)


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except (NotFoundError, NotAssigned) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (StaleRevision, CommandIdReused) as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidDecision as e:
        # FastAPI's validation-error shape, so clients read one 422 format.
        detail = [{"loc": [], "msg": str(e), "type": "invalid_decision"}]
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from e
    except NotLead as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.get("/stages", operation_id="listScreeningStages")
def stages(project_id: Id, ctx: Context) -> list[StageInfo]:
    """Every open stage, with its mode, reveal setting, screeners, auditor, and adjudicators."""
    with ctx.db.transaction() as tx:
        return _screening(ctx).stages(tx, project_id)


@router.post("/stages", operation_id="openScreeningStage", status_code=201)
def open_stage(project_id: Id, body: OpenStageRequest, ctx: Context) -> StageInfo:
    """Open a scope at a stage with the project's people and enqueue the AI judge. Title and
    abstract screens every report of the corpus; full text every record included at title and
    abstract that has a full text."""
    with _errors(), ctx.db.transaction() as tx:
        # Whoever opens a stage hands over its sentinels: only a lead who screens nothing.
        require_lead(tx, project_id, body.actor_id)
        return open_screening(
            tx,
            ctx,
            project_id,
            body.scope_id,
            body.stage,
            actor_id=body.actor_id,
            reveal=body.reveal,
            topic_id=body.topic_id,
            sentinels=body.sentinels,
        )


@router.post("/sentinels/parse", operation_id="parseScreeningSentinels")
def parse_sentinels_route(project_id: Id, body: SentinelFile) -> list[Sentinel]:  # noqa: ARG001
    """Read a sentinel file for `openScreeningStage`; 422 with the line or entry at fault."""
    try:
        return list(parse_sentinels(body.text))
    except LifecycleError as e:
        detail = [{"loc": ["body", "text"], "msg": str(e), "type": "invalid_file"}]
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from e


@router.get("/assignments/{assignment_id}/full-text", operation_id="getScreeningAssignmentFullText")
def assignment_full_text(
    project_id: Id, assignment_id: Id, reviewer: Reviewer, ctx: Context
) -> AssignmentFullText:
    """The full text of one of the caller's full-text assignments, with the protocol-term
    mentions of each criterion in document order."""
    screening = _screening(ctx)
    with _errors(), ctx.db.transaction() as tx:
        ref, _, criteria = screening.assignment_source(tx, project_id, reviewer, assignment_id)
        found = full_text(tx, project_id, ref)
        if found is None:
            raise NotFoundError("this assignment has no full text")
        layer = get_layer(tx, found.layer_id, artifacts=ctx.artifacts)
    return AssignmentFullText(
        document_id=found.document_id,
        layer_id=found.layer_id,
        page_count=found.page_count,
        licence=found.licence,
        mentions=mentions(layer, criteria),
    )


@router.get("/queue", operation_id="getScreeningQueue")
def queue(
    project_id: Id,
    reviewer: Reviewer,
    ctx: Context,
    scope_id: str,
    stage: Stage,
    topic_id: TopicId = None,
    after: Annotated[Id | None, Query(description="The last assignment the client holds.")] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 5,
) -> list[QueueItem]:
    """The reviewer's next undecided assignments in queue order, after `after`. Items can appear
    behind `after`; re-read without it when `remaining` in the stage status is higher than the
    items held."""
    screening = _screening(ctx)
    with _errors(), ctx.db.transaction() as tx:
        return screening.queue(
            tx, project_id, reviewer, scope_id, stage, after=after, limit=limit, topic_id=topic_id
        )


@router.post("/assignments/{assignment_id}/decide", operation_id="decideScreeningAssignment")
def decide(
    project_id: Id, assignment_id: Id, body: DecideRequest, reviewer: Reviewer, ctx: Context
) -> DecideResult:
    """A provisional decision, then a final one if the AI was revealed."""
    screening = _screening(ctx)
    with _errors(), ctx.db.transaction() as tx:
        return screening.decide(
            tx,
            command_id=body.command_id,
            project_id=project_id,
            reviewer_id=reviewer,
            assignment_id=assignment_id,
            decision=body.decision,
            phase=body.phase,
            expected_etag=body.expected_etag,
            timing=body.timing,
        )


@router.post("/assignments/{assignment_id}/undo", operation_id="undoScreeningDecision")
def undo(
    project_id: Id, assignment_id: Id, body: UndoRequest, reviewer: Reviewer, ctx: Context
) -> UndoResult:
    screening = _screening(ctx)
    with _errors(), ctx.db.transaction() as tx:
        return screening.undo(
            tx,
            command_id=body.command_id,
            project_id=project_id,
            reviewer_id=reviewer,
            assignment_id=assignment_id,
            expected_etag=body.expected_etag,
        )


@router.get("/adjudications", operation_id="listScreeningAdjudications")
def adjudications(
    project_id: Id,
    reviewer: Reviewer,
    ctx: Context,
    scope_id: str,
    stage: Stage,
    topic_id: TopicId = None,
) -> list[AdjudicationItem]:
    """Open conflicts the caller did not screen. The AI column opens after the decision."""
    screening = _screening(ctx)
    with _errors(), ctx.db.transaction() as tx:
        return screening.adjudication_queue(
            tx, project_id, reviewer, scope_id, stage, topic_id=topic_id
        )


@router.post("/adjudications/{conflict_id}/decide", operation_id="decideScreeningAdjudication")
def adjudicate(
    project_id: Id, conflict_id: Id, body: AdjudicateRequest, reviewer: Reviewer, ctx: Context
) -> AdjudicationResult:
    screening = _screening(ctx)
    with _errors(), ctx.db.transaction() as tx:
        return screening.adjudicate(
            tx,
            command_id=body.command_id,
            project_id=project_id,
            adjudicator_id=reviewer,
            conflict_id=conflict_id,
            expected_etag=body.expected_etag,
            adopted=body.adopted,
            decision=body.decision,
            timing=body.timing,
        )


@router.get("/status", operation_id="getScreeningStatus")
def status_route(
    project_id: Id,
    reviewer: Reviewer,
    ctx: Context,
    scope_id: str,
    stage: Stage,
    topic_id: TopicId = None,
) -> ScopeStatus:
    """A screener's own counts, or the whole stage for anyone who does not screen it."""
    screening = _screening(ctx)
    with _errors(), ctx.db.transaction() as tx:
        return screening.status(tx, project_id, reviewer, scope_id, stage, topic_id=topic_id)
