"""Protocol routes: validate a source, save a draft, publish it, and read versions back."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status

from kittum.api import Context  # noqa: TC001 - FastAPI resolves it at runtime
from kittum.core import Id, NotFoundError, Record
from kittum.protocol import (
    ProtocolError,
    ProtocolIssue,
    ProtocolStateError,
    StoredProtocol,
    compile,
    get_version,
    list_versions,
    publish,
    save_draft,
)

router = APIRouter(prefix="/api", tags=["protocol"])

type ProtocolSource = dict[str, Any]


class ValidationReport(Record):
    ok: bool
    content_hash: str | None = None
    issues: tuple[ProtocolIssue, ...] = ()


class PublishRequest(Record):
    approved_by: Id
    reason: str | None = None


class ProtocolVersionSummary(Record):
    id: Id
    parent_id: Id | None
    status: Literal["draft", "published"]
    version_label: str
    content_hash: str
    created_at: datetime
    published_at: datetime | None


def _invalid(e: ProtocolError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": "the protocol does not compile",
            "issues": [i.model_dump() for i in e.issues],
        },
    )


@router.post("/protocol/validate", operation_id="validateProtocol")
def validate_protocol(source: ProtocolSource) -> ValidationReport:
    try:
        version = compile(source)
    except ProtocolError as e:
        return ValidationReport(ok=False, issues=e.issues)
    return ValidationReport(ok=True, content_hash=version.content_hash)


@router.post(
    "/projects/{project_id}/protocol-versions",
    operation_id="saveProtocolDraft",
    status_code=status.HTTP_201_CREATED,
)
def save_protocol_draft(project_id: Id, source: ProtocolSource, ctx: Context) -> StoredProtocol:
    try:
        version = compile(source)
    except ProtocolError as e:
        raise _invalid(e) from None
    with ctx.db.transaction() as tx:
        return save_draft(tx, project_id, version)


@router.post(
    "/projects/{project_id}/protocol-versions/{version_id}/publish",
    operation_id="publishProtocolVersion",
)
def publish_protocol_version(
    project_id: Id, version_id: Id, request: PublishRequest, ctx: Context
) -> StoredProtocol:
    if ctx.ledger is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no ledger is configured")
    try:
        with ctx.db.transaction() as tx:
            return publish(
                tx,
                project_id,
                version_id,
                request.approved_by,
                ledger=ctx.ledger,
                reason=request.reason,
            )
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    except ProtocolStateError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None


@router.get("/projects/{project_id}/protocol-versions", operation_id="listProtocolVersions")
def list_protocol_versions(project_id: Id, ctx: Context) -> list[ProtocolVersionSummary]:
    with ctx.db.transaction() as tx:
        stored = list_versions(tx, project_id)
    return [
        ProtocolVersionSummary(
            id=s.id,
            parent_id=s.parent_id,
            status=s.status,
            version_label=s.version.protocol.version_label,
            content_hash=s.version.content_hash,
            created_at=s.created_at,
            published_at=s.published_at,
        )
        for s in stored
    ]


@router.get(
    "/projects/{project_id}/protocol-versions/{version_id}", operation_id="getProtocolVersion"
)
def get_protocol_version(project_id: Id, version_id: Id, ctx: Context) -> StoredProtocol:
    try:
        with ctx.db.transaction() as tx:
            return get_version(tx, project_id, version_id)
    except NotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
