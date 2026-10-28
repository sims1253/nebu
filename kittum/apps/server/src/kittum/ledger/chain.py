"""The hash chain definition.

    payload_hash = SHA-256("kittum.payload.v1" NUL canonical_json(payload))
    event_hash   = SHA-256("kittum.event.v1" NUL canonical_json({
                       project_id, seq, event_id, type, schema_version, actor_id,
                       recorded_at, payload_hash, prev_hash }))

The first event of a project chains to GENESIS_HASH. Changing anything here breaks every existing
chain, so a change needs a new domain tag and a migration plan.
"""

from datetime import UTC, datetime
from typing import Any

from kittum.core import Id, content_hash

GENESIS_HASH = "0" * 64


def payload_hash(payload: dict[str, Any]) -> str:
    return content_hash(payload, domain="kittum.payload.v1")


def event_hash(
    *,
    project_id: Id,
    seq: int,
    event_id: Id,
    type: str,
    schema_version: int,
    actor_id: Id,
    recorded_at: datetime,
    payload_hash: str,
    prev_hash: str,
) -> str:
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    return content_hash(
        {
            "project_id": str(project_id),
            "seq": seq,
            "event_id": str(event_id),
            "type": type,
            "schema_version": schema_version,
            "actor_id": str(actor_id),
            "recorded_at": recorded_at.astimezone(UTC).isoformat(),
            "payload_hash": payload_hash,
            "prev_hash": prev_hash,
        },
        domain="kittum.event.v1",
    )
