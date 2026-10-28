"""The append-only, hash-chained audit ledger (DESIGN.md, "The ledger").

Every module that changes state writes an `EventDraft` in the same transaction as its own rows.
The ledger assigns the sequence number and the chain hash. `chain.event_hash` defines the hash, so
the Postgres store, the SQLite handover bundle, and the offline `verify` tool agree.

Owner: WP-01. Shipped now: the event types, the hash definition, and an in-memory fake in
`kittum.ledger.testing`. Still to build: the Postgres and SQLite stores, checkpoints, and `seal`.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from kittum.core import Id, Record, Sha256
from kittum.ledger.chain import GENESIS_HASH, event_hash, payload_hash

if TYPE_CHECKING:
    from kittum.db import Tx


class EventDraft(Record):
    """An event before the ledger has placed it in the chain."""

    project_id: Id
    type: str
    schema_version: int
    actor_id: Id
    payload: dict[str, Any]


class AuditEvent(Record):
    project_id: Id
    seq: int
    event_id: Id
    type: str
    schema_version: int
    actor_id: Id
    recorded_at: datetime
    payload: dict[str, Any]
    payload_hash: Sha256
    prev_hash: Sha256
    event_hash: Sha256


class VerifyReport(Record):
    project_id: Id
    checked_through_seq: int
    ok: bool
    first_bad_seq: int | None = None
    reason: str | None = None


class Ledger(Protocol):
    """Appends run inside the caller's transaction (see `kittum.db`)."""

    def append(self, tx: Tx, draft: EventDraft) -> AuditEvent: ...

    def events(self, tx: Tx, project_id: Id, *, after_seq: int = 0) -> list[AuditEvent]: ...

    def verify(self, tx: Tx, project_id: Id) -> VerifyReport: ...


__all__ = [
    "GENESIS_HASH",
    "AuditEvent",
    "EventDraft",
    "Ledger",
    "VerifyReport",
    "event_hash",
    "payload_hash",
]
