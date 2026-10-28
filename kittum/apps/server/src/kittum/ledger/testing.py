"""An in-memory ledger for tests in any module. It follows the real chain rules.

It ignores the transaction; pass `kittum.db.testing.NullTx()`.
"""

from typing import TYPE_CHECKING

from kittum.core import Clock, Id, SystemClock, new_id
from kittum.ledger import AuditEvent, EventDraft, VerifyReport
from kittum.ledger.chain import GENESIS_HASH, event_hash, payload_hash

if TYPE_CHECKING:
    from kittum.db import Tx


class InMemoryLedger:
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._events: dict[Id, list[AuditEvent]] = {}

    def append(self, tx: Tx, draft: EventDraft) -> AuditEvent:
        chain = self._events.setdefault(draft.project_id, [])
        seq = len(chain) + 1
        prev = chain[-1].event_hash if chain else GENESIS_HASH
        event_id = new_id()
        recorded_at = self._clock.now()
        p_hash = payload_hash(draft.payload)
        event = AuditEvent(
            project_id=draft.project_id,
            seq=seq,
            event_id=event_id,
            type=draft.type,
            schema_version=draft.schema_version,
            actor_id=draft.actor_id,
            recorded_at=recorded_at,
            payload=draft.payload,
            payload_hash=p_hash,
            prev_hash=prev,
            event_hash=event_hash(
                project_id=draft.project_id,
                seq=seq,
                event_id=event_id,
                type=draft.type,
                schema_version=draft.schema_version,
                actor_id=draft.actor_id,
                recorded_at=recorded_at,
                payload_hash=p_hash,
                prev_hash=prev,
            ),
        )
        chain.append(event)
        return event

    def events(self, tx: Tx, project_id: Id, *, after_seq: int = 0) -> list[AuditEvent]:
        return [e for e in self._events.get(project_id, []) if e.seq > after_seq]

    def verify(self, tx: Tx, project_id: Id) -> VerifyReport:
        prev = GENESIS_HASH
        chain = self._events.get(project_id, [])
        for expected_seq, e in enumerate(chain, start=1):
            recomputed = event_hash(
                project_id=e.project_id,
                seq=e.seq,
                event_id=e.event_id,
                type=e.type,
                schema_version=e.schema_version,
                actor_id=e.actor_id,
                recorded_at=e.recorded_at,
                payload_hash=payload_hash(e.payload),
                prev_hash=prev,
            )
            if e.seq != expected_seq or e.prev_hash != prev or e.event_hash != recomputed:
                return VerifyReport(
                    project_id=project_id,
                    checked_through_seq=expected_seq - 1,
                    ok=False,
                    first_bad_seq=e.seq,
                    reason="chain mismatch",
                )
            prev = e.event_hash
        return VerifyReport(project_id=project_id, checked_through_seq=len(chain), ok=True)
