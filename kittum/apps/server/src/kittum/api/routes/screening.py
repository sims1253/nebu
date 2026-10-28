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
from kittum.core import Id, NotFoundError, Record
from kittum.protocol import Stage  # noqa: TC001
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
    ScopeStatus,
    Screening,
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
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from e


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
    """The reviewer's next undecided assignments in queue order, after `after`."""
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
