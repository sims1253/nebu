"""Protocol assistant routes: draft a protocol from a description, improve one part, accept some
of the suggested changes, step through and restore revisions, count a strategy's hits, and
publish a revision. Every route is a project lead's, as writing the protocol is."""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import Field

from kittum import assist
from kittum.api import Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.api.routes.projects import Viewer  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.app import (
    AssistUnavailable,
    NotLead,
    count_query,
    draft_protocol,
    improve_part,
    require_project_lead,
)
from kittum.core import Id, NotFoundError, Record
from kittum.gateway import GatewayError
from kittum.protocol import ProtocolDiff, ProtocolStateError, StoredProtocol
from kittum.search import QueryCount, SourceError, failure_message

if TYPE_CHECKING:
    from collections.abc import Generator

router = APIRouter(prefix="/api/projects/{project_id}/assist", tags=["assist"])


class DraftRequest(Record):
    description: str = Field(min_length=1)
    """The review in the lead's words: question, population, intervention, comparator, outcomes,
    study designs, languages and dates."""
    language: str = "en"


class ImproveRequest(Record):
    part: str
    """A question (`questions[ID]`), a criterion (`search_topics[T].criteria[ID]`), or a source's
    strategy (`search_topics[T].source_plan.sources[ID].strategy`)."""


class AcceptRequest(Record):
    change_ids: list[str] = Field(min_length=1)


class EditRequest(Record):
    source: dict[str, Any]
    base_id: Id | None = None


class PublishRevisionRequest(Record):
    reason: str | None = None


class AssistModel(Record):
    """The model the assistant asks, so the lead knows who is answering and what they wait for."""

    configured: bool
    recipe_id: str | None = None
    endpoint: str | None = None
    """Where the model server is; None for recorded answers."""


class CountRequest(Record):
    dialect: Literal["pubmed", "ctgov"]
    strategy: str = Field(min_length=1)


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except NotLead as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from None
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    except assist.AssistError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"message": str(e), "issues": [i.model_dump() for i in e.issues]},
        ) from None
    except ProtocolStateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except AssistUnavailable as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from None
    except (SourceError, GatewayError) as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, failure_message(e)) from None


@router.get("/revisions", operation_id="listAssistRevisions")
def list_revisions(project_id: Id, viewer: Viewer, ctx: Context) -> list[assist.ProtocolRevision]:
    """Every revision, oldest first."""
    with _errors(), ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, viewer)
        return assist.list_revisions(tx, project_id)


@router.get("/model", operation_id="getAssistModel")
def model(project_id: Id, viewer: Viewer, ctx: Context) -> AssistModel:
    with _errors(), ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, viewer)
    if ctx.assistant is None:
        return AssistModel(configured=False)
    return AssistModel(
        configured=True,
        recipe_id=ctx.assistant.recipe_id,
        endpoint=getattr(ctx.assistant.backend, "endpoint", None),
    )


@router.post("/draft", operation_id="draftProtocol")
def draft(project_id: Id, body: DraftRequest, viewer: Viewer, ctx: Context) -> assist.AssistOutcome:
    """Ask the model for a first protocol. A draft that compiles becomes a new revision; one that
    still does not after the repair rounds comes back with its problems and the raw answer."""
    with _errors():
        return draft_protocol(ctx, project_id, viewer, body.description, body.language)


@router.post("/revisions/{revision_id}/improve", operation_id="improveProtocolPart")
def improve(
    project_id: Id, revision_id: Id, body: ImproveRequest, viewer: Viewer, ctx: Context
) -> assist.AssistOutcome:
    """Ask the model to improve one part of a revision. Its changes, each with a rationale, become
    a suggestion revision; accept some or all of them with `acceptSuggestedChanges`."""
    with _errors():
        return improve_part(ctx, project_id, viewer, revision_id, body.part)


@router.post(
    "/revisions/{revision_id}/accept",
    operation_id="acceptSuggestedChanges",
    status_code=status.HTTP_201_CREATED,
)
def accept(
    project_id: Id, revision_id: Id, body: AcceptRequest, viewer: Viewer, ctx: Context
) -> assist.ProtocolRevision:
    """The suggestion's base with the chosen changes, as a new revision."""
    with _errors(), ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, viewer)
        return assist.accept(tx, project_id, viewer, revision_id, body.change_ids)


@router.post(
    "/revisions/{revision_id}/restore",
    operation_id="restoreAssistRevision",
    status_code=status.HTTP_201_CREATED,
)
def restore(
    project_id: Id, revision_id: Id, viewer: Viewer, ctx: Context
) -> assist.ProtocolRevision:
    with _errors(), ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, viewer)
        return assist.restore(tx, project_id, viewer, revision_id)


@router.post("/revisions", operation_id="saveAssistRevision", status_code=status.HTTP_201_CREATED)
def save_revision(
    project_id: Id, body: EditRequest, viewer: Viewer, ctx: Context
) -> assist.ProtocolRevision:
    """The lead's own text as a revision. 422 with every problem if it does not compile."""
    with _errors(), ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, viewer)
        return assist.save_edit(tx, project_id, viewer, body.source, base_id=body.base_id)


@router.get("/revisions/{revision_id}/diff/{other_id}", operation_id="compareAssistRevisions")
def compare(
    project_id: Id, revision_id: Id, other_id: Id, viewer: Viewer, ctx: Context
) -> ProtocolDiff:
    """What changes from one revision to the other."""
    with _errors(), ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, viewer)
        return assist.compare(tx, project_id, revision_id, other_id)


@router.post("/revisions/{revision_id}/publish", operation_id="publishAssistRevision")
def publish(
    project_id: Id,
    revision_id: Id,
    body: PublishRevisionRequest,
    viewer: Viewer,
    ctx: Context,
) -> StoredProtocol:
    """Publish a revision with the lead's approval. The ledger event names every field a model
    wrote."""
    with _errors(), ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, viewer)
        return assist.publish_revision(
            tx, project_id, revision_id, viewer, ledger=ctx.require_ledger(), reason=body.reason
        )


@router.post("/count", operation_id="countStrategyHits")
def count(project_id: Id, body: CountRequest, viewer: Viewer, ctx: Context) -> QueryCount:
    """How many records a PubMed or ClinicalTrials.gov strategy finds today. Informational: it
    runs no search and records none."""
    with _errors():
        return count_query(ctx, project_id, viewer, body.dialect, body.strategy)
