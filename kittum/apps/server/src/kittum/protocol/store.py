"""Protocol versions in Postgres: save a draft, publish it with an approval, read it back.

Publishing freezes the version (a trigger rejects later changes) and appends a
`protocol.published` event in the caller's transaction. A version that changes a topic's screening
mode relative to the last published version needs a reason, which the event records. So does every
field of the version a model wrote (`ai_parts`): which field, from which revision, by which model.
Publishing asks every registered attribution (`register_attribution`; the protocol assistant
registers one at the entry points) whatever saved the version: the workbench, an assistant
revision, or the CLI.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from psycopg.types.json import Jsonb

from kittum.core import Id, KittumError, NotFoundError, Record
from kittum.ledger import EventDraft
from kittum.protocol.compiler import ProtocolVersion, compile
from kittum.protocol.diffing import diff

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from kittum.db import Tx
    from kittum.ledger import AuditEvent, Ledger

PUBLISHED_EVENT = "protocol.published"

type Attribution = Callable[[Tx, Id, ProtocolVersion], Sequence[Mapping[str, Any]]]
"""The fields of a version that a model wrote, as `protocol.published` records them."""

_ATTRIBUTIONS: dict[str, Attribution] = {}


def register_attribution(name: str, attribution: Attribution) -> None:
    """Have every publication record what `attribution` finds. Registering a name again replaces
    its function."""
    _ATTRIBUTIONS[name] = attribution


def attributed(tx: Tx, project_id: Id, version: ProtocolVersion) -> list[dict[str, Any]]:
    return [dict(p) for f in _ATTRIBUTIONS.values() for p in f(tx, project_id, version)]


class ProtocolStateError(KittumError):
    """The request does not fit the version's state, e.g. publishing it twice."""


class StoredProtocol(Record):
    id: Id
    project_id: Id
    parent_id: Id | None
    status: Literal["draft", "published"]
    version: ProtocolVersion
    created_at: datetime
    published_at: datetime | None = None
    approved_by: Id | None = None
    approved_event_id: Id | None = None
    publish_reason: str | None = None


class ProtocolStore(Protocol):
    """The persistence seam. `PostgresProtocolStore` and `testing.InMemoryProtocolStore` fit it."""

    def save_draft(self, tx: Tx, project_id: Id, version: ProtocolVersion) -> StoredProtocol: ...

    def publish(
        self,
        tx: Tx,
        project_id: Id,
        version_id: Id,
        approved_by: Id,
        *,
        reason: str | None = None,
    ) -> StoredProtocol: ...

    def get_version(self, tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol: ...

    def list_versions(self, tx: Tx, project_id: Id) -> list[StoredProtocol]: ...

    def latest_published(self, tx: Tx, project_id: Id) -> StoredProtocol | None: ...


def published_payload(
    stored: StoredProtocol,
    previous: StoredProtocol | None,
    reason: str | None,
    ai_parts: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The `protocol.published` payload. Shared with the in-memory store."""
    changes = diff(previous.version, stored.version) if previous else None
    return {
        "protocol_version_id": str(stored.id),
        "content_hash": stored.version.content_hash,
        "version_label": stored.version.protocol.version_label,
        "previous_version_id": str(previous.id) if previous else None,
        "previous_content_hash": previous.version.content_hash if previous else None,
        "reason": reason,
        "mode_changes": [m.model_dump(mode="json") for m in changes.mode_changes]
        if changes
        else [],
        "added_criteria": list(changes.added_criteria) if changes else [],
        "removed_criteria": list(changes.removed_criteria) if changes else [],
        "changed_criteria": list(changes.changed_criteria) if changes else [],
        "ai_parts": [dict(p) for p in ai_parts],
    }


def check_publishable(
    stored: StoredProtocol, previous: StoredProtocol | None, reason: str | None
) -> None:
    if stored.status == "published":
        raise ProtocolStateError(f"protocol version {stored.id} is already published")
    if previous and not (reason and reason.strip()):
        changed = diff(previous.version, stored.version).mode_changes
        if changed:
            topics = ", ".join(f"{m.topic_id} {m.before}→{m.after}" for m in changed)
            raise ProtocolStateError(f"changing a screening mode needs a reason ({topics})")


def published_event(
    stored: StoredProtocol,
    previous: StoredProtocol | None,
    approved_by: Id,
    reason: str | None,
    ai_parts: Sequence[Mapping[str, Any]] = (),
) -> EventDraft:
    return EventDraft(
        project_id=stored.project_id,
        type=PUBLISHED_EVENT,
        schema_version=1,
        actor_id=approved_by,
        payload=published_payload(stored, previous, reason, ai_parts),
    )


def mark_published(
    stored: StoredProtocol, event: AuditEvent, approved_by: Id, reason: str | None
) -> StoredProtocol:
    return stored.model_copy(
        update={
            "status": "published",
            "published_at": event.recorded_at,
            "approved_by": approved_by,
            "approved_event_id": event.event_id,
            "publish_reason": reason,
        }
    )


# Postgres

_COLUMNS = (
    "id, project_id, parent_id, status, content_hash, document, created_at, published_at,"
    " approved_by, approved_event_id, publish_reason"
)


def _from_row(row: dict[str, Any]) -> StoredProtocol:
    version = compile(row["document"])
    if version.content_hash != row["content_hash"]:
        raise KittumError(f"protocol version {row['id']} does not match its content hash")
    return StoredProtocol(
        id=row["id"],
        project_id=row["project_id"],
        parent_id=row["parent_id"],
        status=row["status"],
        version=version,
        created_at=row["created_at"],
        published_at=row["published_at"],
        approved_by=row["approved_by"],
        approved_event_id=row["approved_event_id"],
        publish_reason=row["publish_reason"],
    )


def save_draft(tx: Tx, project_id: Id, version: ProtocolVersion) -> StoredProtocol:
    """Store a compiled version as a draft. Saving the same content again returns the stored row."""
    row = tx.execute(
        f"select {_COLUMNS} from protocol_version where project_id = %s and content_hash = %s",
        (project_id, version.content_hash),
    ).fetchone()
    if row is None:
        parent = latest_published(tx, project_id)
        row = tx.execute(
            f"insert into protocol_version (project_id, parent_id, version_label, content_hash,"
            f" document) values (%s, %s, %s, %s, %s) returning {_COLUMNS}",
            (
                project_id,
                parent.id if parent else None,
                version.protocol.version_label,
                version.content_hash,
                Jsonb(version.protocol.model_dump(mode="json")),
            ),
        ).fetchone()
        assert row is not None
    return _from_row(row)


def publish(
    tx: Tx,
    project_id: Id,
    version_id: Id,
    approved_by: Id,
    *,
    ledger: Ledger,
    reason: str | None = None,
) -> StoredProtocol:
    """Freeze a draft with a human approval and append `protocol.published`, naming the fields a
    model wrote (every registered attribution) for the AI disclosure.

    Publications serialize per project, so the version compared against is the one in force when
    this one commits: two concurrent publications cannot both skip the mode-change reason.
    """
    tx.execute(
        "select pg_advisory_xact_lock(hashtext('kittum.protocol.publish'), hashtext(%s))",
        (str(project_id),),
    )
    row = tx.execute(
        f"select {_COLUMNS} from protocol_version where id = %s and project_id = %s for update",
        (version_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"protocol version {version_id} not found in project {project_id}")
    stored = _from_row(row)
    actor = tx.execute("select kind from actor where id = %s", (approved_by,)).fetchone()
    if actor is None:
        raise ProtocolStateError(f"actor {approved_by} does not exist")
    if actor["kind"] != "human":
        raise ProtocolStateError("publishing a protocol needs the approval of a human actor")
    previous = latest_published(tx, project_id)
    check_publishable(stored, previous, reason)
    ai_parts = attributed(tx, project_id, stored.version)
    event = ledger.append(tx, published_event(stored, previous, approved_by, reason, ai_parts))
    published = mark_published(stored, event, approved_by, reason)
    tx.execute(
        "update protocol_version set status = 'published', published_at = %s, approved_by = %s,"
        " approved_event_id = %s, publish_reason = %s where id = %s",
        (published.published_at, approved_by, event.event_id, reason, version_id),
    )
    return published


def get_version(tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol:
    row = tx.execute(
        f"select {_COLUMNS} from protocol_version where id = %s and project_id = %s",
        (version_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"protocol version {version_id} not found in project {project_id}")
    return _from_row(row)


def list_versions(tx: Tx, project_id: Id) -> list[StoredProtocol]:
    """Every version of a project, oldest first."""
    rows = tx.execute(
        f"select {_COLUMNS} from protocol_version where project_id = %s order by created_at, id",
        (project_id,),
    ).fetchall()
    return [_from_row(r) for r in rows]


def latest_published(tx: Tx, project_id: Id) -> StoredProtocol | None:
    """The version in force: the one published last."""
    row = tx.execute(
        f"select {_COLUMNS} from protocol_version where project_id = %s and status = 'published'"
        " order by published_at desc, id desc limit 1",
        (project_id,),
    ).fetchone()
    return _from_row(row) if row else None


class PostgresProtocolStore:
    """The functions above behind the `ProtocolStore` seam."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger

    def save_draft(self, tx: Tx, project_id: Id, version: ProtocolVersion) -> StoredProtocol:
        return save_draft(tx, project_id, version)

    def publish(
        self,
        tx: Tx,
        project_id: Id,
        version_id: Id,
        approved_by: Id,
        *,
        reason: str | None = None,
    ) -> StoredProtocol:
        return publish(tx, project_id, version_id, approved_by, ledger=self._ledger, reason=reason)

    def get_version(self, tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol:
        return get_version(tx, project_id, version_id)

    def list_versions(self, tx: Tx, project_id: Id) -> list[StoredProtocol]:
        return list_versions(tx, project_id)

    def latest_published(self, tx: Tx, project_id: Id) -> StoredProtocol | None:
        return latest_published(tx, project_id)
