"""Shared helpers for the protocol tests.

`SqlLedger` writes real `audit_event` rows with the chain rules from `kittum.ledger`, so the
publish tests run against Postgres before WP-01 ships its store. Replace it with that store then.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from psycopg.types.json import Jsonb

from kittum.core import Id, SystemClock, new_id
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

EXAMPLES = Path(__file__).resolve().parents[4] / "examples" / "protocols"


def example_text(name: str = "nicotine-replacement") -> str:
    return (EXAMPLES / f"{name}.yaml").read_text()


def example_source(name: str = "nicotine-replacement") -> dict[str, Any]:
    """The example as JSON data: YAML dates become ISO strings, as in a request body."""
    return json.loads(json.dumps(yaml.safe_load(example_text(name)), default=str))


def seed_project(tx: Tx) -> tuple[Id, Id, Id]:
    """A project, a human approver, and a system actor."""
    project = tx.execute(
        "insert into project (name, policy_version) values ('example review', 'p1') returning id"
    ).fetchone()
    human = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'human') returning id",
        (f"reviewer-{new_id()}",),
    ).fetchone()
    system = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'system') returning id",
        (f"system-{new_id()}",),
    ).fetchone()
    assert project and human and system
    return project["id"], human["id"], system["id"]


class SqlLedger:
    def __init__(self) -> None:
        self._clock = SystemClock()

    def append(self, tx: Tx, draft: EventDraft) -> AuditEvent:
        tx.execute("select pg_advisory_xact_lock(hashtext(%s))", (str(draft.project_id),))
        last = tx.execute(
            "select seq, event_hash from audit_event where project_id = %s"
            " order by seq desc limit 1",
            (draft.project_id,),
        ).fetchone()
        seq = last["seq"] + 1 if last else 1
        prev = last["event_hash"] if last else GENESIS_HASH
        event_id = new_id()
        # Postgres keeps microseconds, so the hash must use what it stores.
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
        tx.execute(
            "insert into audit_event (project_id, seq, event_id, type, schema_version, actor_id,"
            " recorded_at, payload, payload_hash, prev_hash, event_hash)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                draft.project_id,
                seq,
                event_id,
                draft.type,
                draft.schema_version,
                draft.actor_id,
                recorded_at,
                Jsonb(draft.payload),
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
        rows = tx.execute(
            "select * from audit_event where project_id = %s and seq > %s order by seq",
            (project_id, after_seq),
        ).fetchall()
        return [AuditEvent.model_validate(r) for r in rows]

    def verify(self, tx: Tx, project_id: Id) -> VerifyReport:
        prev = GENESIS_HASH
        events = self.events(tx, project_id)
        for e in events:
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
            if e.prev_hash != prev or e.event_hash != recomputed:
                return VerifyReport(
                    project_id=project_id,
                    checked_through_seq=e.seq - 1,
                    ok=False,
                    first_bad_seq=e.seq,
                    reason="chain mismatch",
                )
            prev = e.event_hash
        return VerifyReport(project_id=project_id, checked_through_seq=len(events), ok=True)
