"""Project routes: projects and their people, full texts, and progress.

Until the API has authentication, the caller of a read that depends on who asks names themselves
in `X-Kittum-Reviewer`, as in the screening routes. It is a stand-in, not a security boundary.
"""

from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool

from kittum.api import AppContext, Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.app import (
    FullText,
    LifecycleError,
    Member,
    Progress,
    ProjectSummary,
    Role,
    add_member,
    attach_full_text,
    create_project,
    list_projects,
    progress,
    project_summary,
)
from kittum.core import Id, NotFoundError, Record
from kittum.documents import NotAPdf
from kittum.screening import NotAssigned

if TYPE_CHECKING:
    from collections.abc import Generator

router = APIRouter(prefix="/api/projects", tags=["projects"])

_PDF_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
    }
}

Viewer = Annotated[
    Id,
    Header(
        alias="X-Kittum-Reviewer",
        description="The caller's actor ID. A stand-in until the API has authentication.",
    ),
]


class CreateProjectRequest(Record):
    name: str
    lead_name: str


class AddMemberRequest(Record):
    display_name: str
    roles: tuple[Role, ...]
    added_by: Id


@contextmanager
def _errors() -> Generator[None]:
    try:
        yield
    except (NotFoundError, NotAssigned) as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except LifecycleError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e


@router.get("", operation_id="listProjects")
def list_projects_route(ctx: Context) -> list[ProjectSummary]:
    with ctx.db.transaction() as tx:
        return list_projects(tx, ctx.screening())


@router.post("", operation_id="createProject", status_code=status.HTTP_201_CREATED)
def create_project_route(body: CreateProjectRequest, ctx: Context) -> ProjectSummary:
    """A project and its methods lead, who adds the other people."""
    with ctx.db.transaction() as tx:
        project_id, _ = create_project(
            tx, name=body.name, lead_name=body.lead_name, ledger=ctx.require_ledger()
        )
        return project_summary(tx, project_id, ctx.screening())


@router.get("/{project_id}", operation_id="getProject")
def get_project(project_id: Id, ctx: Context) -> ProjectSummary:
    with _errors(), ctx.db.transaction() as tx:
        return project_summary(tx, project_id, ctx.screening())


@router.post("/{project_id}/members", operation_id="addProjectMember", status_code=201)
def add_member_route(project_id: Id, body: AddMemberRequest, ctx: Context) -> Member:
    with _errors(), ctx.db.transaction() as tx:
        return add_member(
            tx,
            project_id,
            display_name=body.display_name,
            roles=body.roles,
            added_by=body.added_by,
            ledger=ctx.require_ledger(),
        )


def _attach(
    ctx: AppContext, project_id: Id, item_ref: str, pdf: bytes, actor_id: Id, licence: str | None
) -> FullText:
    with ctx.db.transaction() as tx:
        return attach_full_text(
            tx,
            project_id,
            item_ref,
            pdf,
            actor_id=actor_id,
            artifacts=ctx.artifacts,
            ledger=ctx.require_ledger(),
            licence=licence,
        )


@router.post(
    "/{project_id}/full-texts/{item_ref}",
    operation_id="attachFullText",
    status_code=201,
    openapi_extra=_PDF_BODY,
)
async def attach_full_text_route(
    project_id: Id,
    item_ref: str,
    request: Request,
    ctx: Context,
    actor_id: Annotated[Id, Query()],
    licence: Annotated[str | None, Query()] = None,
) -> FullText:
    """Upload a record's full text (the human end of the full-text waterfall)."""
    pdf = await request.body()
    try:
        with _errors():
            return await run_in_threadpool(
                _attach, ctx, project_id, item_ref, pdf, actor_id, licence
            )
    except NotAPdf as e:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"not a PDF: {e}") from e


@router.get("/{project_id}/progress", operation_id="getProjectProgress")
def get_progress(project_id: Id, viewer: Viewer, ctx: Context) -> Progress:
    """Identification counts and every stage as the viewer may see it: a screener their own
    counts, an adjudicator the progress and their conflicts, the methods lead everything."""
    with _errors(), ctx.db.transaction() as tx:
        return progress(tx, project_id, viewer, ctx.screening())
