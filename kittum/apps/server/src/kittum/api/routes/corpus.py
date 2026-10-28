"""Corpus routes for proposals, human confirmation, split, merge, and PRISMA counts."""

from contextlib import contextmanager
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query

from kittum.api import AppContext, Context  # noqa: TC001
from kittum.api.routes.projects import Viewer  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.app import LifecycleError, NotLead, ingest_corpus, require_lead
from kittum.core import Id, NotFoundError, Record
from kittum.corpus import (
    CorpusReport,
    CorpusRevision,
    IdentityRevision,
    LinkProposal,
    PrismaCounts,
    ProposalStatus,
    Relation,
    SplitLevel,
    confirm,
    get_counts,
    latest_revision,
    list_proposals,
    merge,
    reports,
    split,
)
from kittum.ledger import Ledger  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Generator

router = APIRouter(prefix="/api/projects/{project_id}/corpus", tags=["corpus"])


class IngestRequest(Record):
    actor_id: Id


class CorpusView(Record):
    """The newest identity revision: every report with its records and study."""

    revision: IdentityRevision
    reports: tuple[CorpusReport, ...]


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
    level: SplitLevel
    actor_id: Id
    reason: str = ""


@router.post("/ingest", operation_id="ingestCorpus", status_code=201)
def ingest_route(project_id: Id, body: IngestRequest, ctx: Context) -> CorpusRevision:
    """Deduplicate every search of the project into the first identity revision."""
    with _errors(), ctx.db.transaction() as tx:
        # The revision comes back with every record: the lead's catalogue.
        require_lead(tx, project_id, body.actor_id)
        return ingest_corpus(tx, project_id, actor_id=body.actor_id, ledger=_ledger(ctx))


@router.get("/revisions/latest", operation_id="getLatestCorpusRevision")
def latest_route(project_id: Id, viewer: Viewer, ctx: Context) -> CorpusView:
    """Every record of the project, for its methods lead only (403 otherwise)."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
        revision = latest_revision(tx, project_id)
        if revision is None:
            raise NotFoundError("the corpus is not ingested yet")
        return CorpusView(revision=revision, reports=tuple(reports(tx, project_id, revision.id)))


@router.get("/proposals", operation_id="listCorpusProposals")
def list_proposals_route(
    project_id: Id,
    viewer: Viewer,
    ctx: Context,
    corpus_revision_id: Id | None = Query(default=None),
    status: ProposalStatus | None = Query(default=None),
) -> list[LinkProposal]:
    """The link proposals, for the project's methods lead only (403 otherwise)."""
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, viewer)
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
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        return confirm(
            tx,
            project_id,
            body.revision_id,
            body.proposal_id,
            body.actor_id,
            ledger=_ledger(ctx),
        )


@router.post("/merge", operation_id="mergeCorpusOccurrences")
def merge_route(
    project_id: Id,
    body: MergeRequest,
    ctx: Context,
) -> IdentityRevision:
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        return merge(
            tx,
            project_id,
            body.revision_id,
            body.occurrence_ids,
            body.relation,
            body.actor_id,
            reason=body.reason,
            ledger=_ledger(ctx),
        )


@router.post("/split", operation_id="splitCorpusOccurrences")
def split_route(
    project_id: Id,
    body: SplitRequest,
    ctx: Context,
) -> IdentityRevision:
    with _errors(), ctx.db.transaction() as tx:
        require_lead(tx, project_id, body.actor_id)
        return split(
            tx,
            project_id,
            body.revision_id,
            body.occurrence_ids,
            body.level,
            body.actor_id,
            reason=body.reason,
            ledger=_ledger(ctx),
        )


@router.get("/revisions/{revision_id}/counts", operation_id="getCorpusPrismaCounts")
def get_counts_route(
    project_id: Id,
    revision_id: Id,
    ctx: Context,
) -> PrismaCounts:
    with _errors(), ctx.db.transaction() as tx:
        return get_counts(tx, project_id, revision_id)


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except NotLead as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _ledger(ctx: AppContext) -> Ledger:
    if ctx.ledger is None:
        raise HTTPException(status_code=503, detail="no ledger is configured")
    return ctx.ledger
