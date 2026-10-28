"""Audit routes: the ledger as an assessor reads it.

One record's history from search to extraction, the ledger a page at a time, a full integrity
check that names every failing event and check, the signed checkpoints, named cutoffs, and the
PRISMA flows and AI disclosure as of any cutoff. Every answer carries reviewers' decisions or the
AI's readings, so every route is for the methods lead only (`report.require_lead`): never a
screener, auditor, or adjudicator of a stage. Until the API has authentication (WP-15) the
caller names the actor in the `X-Kittum-Reviewer` header; that is a stand-in, not a security
boundary.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Response

from kittum.api import AppContext, Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.core import Id, NotFoundError, Record
from kittum.ledger import (
    ChainAudit,
    CheckpointStatus,
    LedgerIntegrityError,
    SigningKey,
    audit_chain,
)
from kittum.report import (
    ArtifactCheck,
    AsOf,
    EventPage,
    Milestone,
    NotTheLead,
    RecordTrace,
    ReportRequestError,
    as_of,
    check_artifact,
    events,
    media_type,
    milestones,
    referenced,
    require_lead,
    trace,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from kittum.ledger import Ledger, VerifyKey

router = APIRouter(prefix="/api/projects/{project_id}/audit", tags=["audit"])

Sha256Path = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
Reviewer = Annotated[
    Id,
    Header(
        alias="X-Kittum-Reviewer",
        description="The caller's actor ID. A stand-in until the API has authentication.",
    ),
]


class SignCheckpointRequest(Record):
    actor_id: Id


def _ledger(ctx: AppContext) -> Ledger:
    if ctx.ledger is None:
        raise HTTPException(status_code=503, detail="no ledger is configured")
    return ctx.ledger


def _signer(ctx: AppContext) -> SigningKey | None:
    path = ctx.settings.checkpoint_key_path
    return SigningKey.from_file(path) if path is not None else None


def _trusted(ctx: AppContext) -> VerifyKey | None:
    signer = _signer(ctx)
    return signer.verify_key if signer else None


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except NotTheLead as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ReportRequestError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/records/{ref}/trace", operation_id="traceRecord")
def trace_record(project_id: Id, ref: str, reviewer: Reviewer, ctx: Context) -> RecordTrace:
    """Every ledger event that concerns one record, from the searches that found it on.

    `ref` is any reference a PRISMA count lists: a report, a search result, or a study.
    """
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, reviewer)
        return trace(tx, project_id, ref, store=ctx.artifacts, ledger=_ledger(ctx))


@router.get("/events", operation_id="listAuditEvents")
def list_events(
    project_id: Id,
    reviewer: Reviewer,
    ctx: Context,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    through_seq: Annotated[int | None, Query(ge=1)] = None,
    type: Annotated[list[str] | None, Query()] = None,
    actor_id: Id | None = None,
    ref: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventPage:
    """Ledger events after `after_seq`, with their payloads. A `type` ending in "." matches its
    family ("screening."); `ref` keeps one record's events."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, reviewer)
        return events(
            tx,
            project_id,
            after_seq=after_seq,
            through_seq=through_seq,
            types=type or (),
            actor_id=actor_id,
            ref=ref,
            limit=limit,
            store=ctx.artifacts,
            ledger=_ledger(ctx),
        )


@router.get("/verify", operation_id="auditLedger")
def verify(project_id: Id, reviewer: Reviewer, ctx: Context) -> ChainAudit:
    """Recompute every payload hash, event hash, and link, and check every checkpoint against the
    server's checkpoint key. Names each failing event and check, not only the first."""
    ledger = _ledger(ctx)
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, reviewer)
        return audit_chain(
            project_id,
            ledger.events(tx, project_id),
            ledger.checkpoints(tx, project_id),
            key=_trusted(ctx),
        )


@router.get("/checkpoints", operation_id="listCheckpoints")
def list_checkpoints(project_id: Id, reviewer: Reviewer, ctx: Context) -> list[CheckpointStatus]:
    """Every stored checkpoint, oldest first, with its signature checked against the server's key
    and its head hash against the stored event."""
    return list(verify(project_id, reviewer, ctx).checkpoints)


@router.post("/checkpoints", operation_id="signCheckpoint", status_code=201)
def sign_checkpoint(project_id: Id, body: SignCheckpointRequest, ctx: Context) -> CheckpointStatus:
    """Sign the chain head with the server's checkpoint key. Refused if the chain does not verify."""
    ledger = _ledger(ctx)
    signer = _signer(ctx)
    if signer is None:
        raise HTTPException(status_code=503, detail="no checkpoint key is configured")
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        try:
            checkpoint = ledger.checkpoint(tx, project_id, signer)
        except LedgerIntegrityError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
    return CheckpointStatus(checkpoint=checkpoint, signature="valid", head_matches=True)


@router.get("/milestones", operation_id="listMilestones")
def list_milestones(project_id: Id, reviewer: Reviewer, ctx: Context) -> list[Milestone]:
    """Named ledger positions to pick a cutoff by: protocol versions, searches, the corpus, and
    every stage opened and closed."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, reviewer)
        return list(milestones(tx, project_id, ledger=_ledger(ctx)))


@router.get("/as-of", operation_id="reviewAsOf")
def review_as_of(
    project_id: Id,
    reviewer: Reviewer,
    ctx: Context,
    cutoff_seq: Annotated[int | None, Query(ge=0)] = None,
) -> AsOf:
    """The PRISMA flows and the AI disclosure as the ledger stood at `cutoff_seq` (the head when
    empty). Computed, not stored: the Reports screen renders and keeps a report."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, reviewer)
        return as_of(tx, project_id, cutoff_seq, ledger=_ledger(ctx), store=ctx.artifacts)


@router.get(
    "/artifacts/{sha256}",
    operation_id="downloadAuditArtifact",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def download(project_id: Id, sha256: Sha256Path, reviewer: Reviewer, ctx: Context) -> Response:
    """An artifact the project names: a raw search response, a full text, a model call's
    request or response, a rendered prompt. Nothing else by hash."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, reviewer)
        known = referenced(tx, project_id, sha256, ctx.artifacts)
    if not known or not ctx.artifacts.exists(sha256):
        raise HTTPException(status_code=404, detail=f"no event of the project names {sha256}")
    data = ctx.artifacts.get(sha256)
    return Response(
        content=data,
        media_type=media_type(data),
        headers={"Content-Disposition": f'inline; filename="{sha256}"'},
    )


@router.get("/artifacts/{sha256}/check", operation_id="checkAuditArtifact")
def check(project_id: Id, sha256: Sha256Path, reviewer: Reviewer, ctx: Context) -> ArtifactCheck:
    """Read an artifact the project names from the store and hash it again: whether its bytes
    still match the hash the ledger recorded."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, reviewer)
        return check_artifact(tx, project_id, sha256, ctx.artifacts)
