"""Corpus routes for proposals, human confirmation, split, merge, and PRISMA counts."""

from fastapi import APIRouter, HTTPException, Query

from kittum.api import Context  # noqa: TC001
from kittum.core import Id, Record
from kittum.corpus import (
    IdentityRevision,
    LinkProposal,
    PrismaCounts,
    ProposalStatus,
    Relation,
    confirm,
    get_counts,
    list_proposals,
    merge,
    split,
)

router = APIRouter(prefix="/api/projects/{project_id}/corpus", tags=["corpus"])


class ConfirmRequest(Record):
    revision_id: Id
    proposal_id: Id
    actor_id: Id


class MergeRequest(Record):
    revision_id: Id
    occurrence_ids: tuple[Id, ...]
    relation: Relation
    actor_id: Id
    reason: str = ""


class SplitRequest(Record):
    revision_id: Id
    occurrence_ids: tuple[Id, ...]
    actor_id: Id
    reason: str = ""


@router.get("/proposals", operation_id="listCorpusProposals")
def list_proposals_route(
    project_id: Id,
    ctx: Context,
    corpus_revision_id: Id | None = Query(default=None),
    status: ProposalStatus | None = Query(default=None),
) -> list[LinkProposal]:
    with ctx.db.transaction() as tx:
        return list_proposals(
            tx,
            project_id,
            corpus_revision_id=corpus_revision_id,
            status=status,
        )


@router.post("/confirm", operation_id="confirmCorpusProposal")
def confirm_route(
    project_id: Id,
    body: ConfirmRequest,
    ctx: Context,
) -> IdentityRevision:
    try:
        with ctx.db.transaction() as tx:
            return confirm(
                tx,
                project_id,
                body.revision_id,
                body.proposal_id,
                body.actor_id,
                ledger=ctx.ledger,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/merge", operation_id="mergeCorpusOccurrences")
def merge_route(
    project_id: Id,
    body: MergeRequest,
    ctx: Context,
) -> IdentityRevision:
    try:
        with ctx.db.transaction() as tx:
            return merge(
                tx,
                project_id,
                body.revision_id,
                body.occurrence_ids,
                body.relation,
                body.actor_id,
                reason=body.reason,
                ledger=ctx.ledger,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/split", operation_id="splitCorpusOccurrences")
def split_route(
    project_id: Id,
    body: SplitRequest,
    ctx: Context,
) -> IdentityRevision:
    try:
        with ctx.db.transaction() as tx:
            return split(
                tx,
                project_id,
                body.revision_id,
                body.occurrence_ids,
                body.actor_id,
                reason=body.reason,
                ledger=ctx.ledger,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/revisions/{revision_id}/counts", operation_id="getCorpusPrismaCounts")
def get_counts_route(
    project_id: Id,  # noqa: ARG001
    revision_id: Id,
    ctx: Context,
) -> PrismaCounts:
    with ctx.db.transaction() as tx:
        return get_counts(tx, revision_id)
