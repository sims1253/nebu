"""Postgres ledger implementation for corpus operations."""

import json
from typing import TYPE_CHECKING

from kittum.core import Clock, Id, SystemClock, new_id
from kittum.ledger import (
    GENESIS_HASH,
    AuditEvent,
    EventDraft,
    VerifyReport,
    event_hash,
    payload_hash,
)

if TYPE_CHECKING:
    from kittum.db import Tx


class PostgresLedger:
    """An append-only, hash-chained ledger backed by the Postgres audit_event table."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()

    def append(self, tx: Tx, draft: EventDraft) -> AuditEvent:
        with tx.cursor() as cur:
            cur.execute(
                """
                select seq, event_hash
                from audit_event
                where project_id = %s
                order by seq desc
                limit 1
                for update
                """,
                (draft.project_id,),
            )
            row = cur.fetchone()
            if row is None:
                seq = 1
                prev = GENESIS_HASH
            else:
                seq = row["seq"] + 1
                prev = row["event_hash"]

            event_id = new_id()
            recorded_at = self._clock.now()
            p_hash = payload_hash(draft.payload)
            e_hash = event_hash(
                project_id=draft.project_id,
                seq=seq,
                event_id=event_id,
                type=draft.type,
                schema_version=draft.schema_version,
                actor_id=draft.actor_id,
                recorded_at=recorded_at,
                payload_hash=p_hash,
                prev_hash=prev,
            )

            cur.execute(
                """
                insert into audit_event (
                    project_id, seq, event_id, type, schema_version,
                    actor_id, recorded_at, payload, payload_hash, prev_hash, event_hash
                ) values (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s::jsonb, %s, %s, %s
                )
                """,
                (
                    draft.project_id,
                    seq,
                    event_id,
                    draft.type,
                    draft.schema_version,
                    draft.actor_id,
                    recorded_at,
                    json.dumps(draft.payload),
                    p_hash,
                    prev,
                    e_hash,
                ),
            )

        return AuditEvent(
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
            event_hash=e_hash,
        )

    def events(self, tx: Tx, project_id: Id, *, after_seq: int = 0) -> list[AuditEvent]:
        with tx.cursor() as cur:
            cur.execute(
                """
                select
                    project_id, seq, event_id, type, schema_version,
                    actor_id, recorded_at, payload, payload_hash, prev_hash, event_hash
                from audit_event
                where project_id = %s and seq > %s
                order by seq asc
                """,
                (project_id, after_seq),
            )
            rows = cur.fetchall()
            return [AuditEvent(**row) for row in rows]

    def verify(self, tx: Tx, project_id: Id) -> VerifyReport:
        all_events = self.events(tx, project_id)
        prev = GENESIS_HASH
        for expected_seq, e in enumerate(all_events, start=1):
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
        return VerifyReport(project_id=project_id, checked_through_seq=len(all_events), ok=True)
