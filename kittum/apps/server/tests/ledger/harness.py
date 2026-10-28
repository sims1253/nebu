"""One harness per ledger store, so a single behaviour suite runs against all of them.

Each harness can also tamper with its store the way an administrator could: delete rows and
insert forged ones, bypassing the append-only triggers.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from kittum.core import FixedClock, canonical_json, new_id
from kittum.db.testing import null_tx
from kittum.ledger import AuditEvent, EventDraft, PostgresLedger, SqliteLedger
from kittum.ledger.testing import InMemoryLedger, seed_project

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from kittum.core import Id
    from kittum.db import Tx
    from kittum.ledger import Ledger

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

# A payload that exercises JSON round trips: umlauts, nesting, null, booleans, a float, and an
# exact decimal kept as a string.
PAYLOAD: dict[str, Any] = {
    "title": "Lebensqualität und Größe (EQ-5D)",
    "n": 119,
    "lower_bound": 0.692,
    "hazard_ratio": {"value": "0.74", "unit": "ratio"},
    "flags": [True, False, None],
    "note": "",
}


class TickingClock(FixedClock):
    """A fixed clock that moves forward by a microsecond-odd step on every read."""

    def now(self) -> datetime:
        current = super().now()
        self.advance(timedelta(seconds=1, microseconds=123))
        return current


@dataclass
class Harness:
    name: str
    ledger: Ledger
    tx: Tx
    clock: TickingClock
    new_project: Callable[[], tuple[Id, Id]]
    delete: Callable[[Id, int], None]
    insert: Callable[[AuditEvent], None]
    overwrite: Callable[[AuditEvent], None]
    """Replace the stored event at the same seq in place, leaving checkpoints alone."""

    def append(self, project_id: Id, actor_id: Id, n: int, kind: str = "test.event") -> None:
        for i in range(n):
            self.ledger.append(
                self.tx,
                EventDraft(
                    project_id=project_id,
                    type=kind,
                    schema_version=1,
                    actor_id=actor_id,
                    payload={**PAYLOAD, "i": i},
                ),
            )


def _ids() -> tuple[Id, Id]:
    return new_id(), new_id()


def memory_harness() -> Iterator[Harness]:
    clock = TickingClock(START)
    ledger = InMemoryLedger(clock)

    def delete(project_id: Id, seq: int) -> None:
        ledger._events[project_id] = [e for e in ledger._events[project_id] if e.seq != seq]

    def insert(event: AuditEvent) -> None:
        chain = ledger._events.setdefault(event.project_id, [])
        chain.append(event)
        chain.sort(key=lambda e: e.seq)

    def overwrite(event: AuditEvent) -> None:
        chain = ledger._events[event.project_id]
        chain[:] = [event if e.seq == event.seq else e for e in chain]

    yield Harness("memory", ledger, null_tx(), clock, _ids, delete, insert, overwrite)


def sqlite_harness(tmp_path: Path) -> Iterator[Harness]:
    clock = TickingClock(START)
    ledger = SqliteLedger.open(tmp_path / "ledger.sqlite", clock=clock)
    conn = ledger.connection

    def unlocked() -> None:
        conn.execute("drop trigger if exists audit_event_no_update")
        conn.execute("drop trigger if exists audit_event_no_delete")

    def delete(project_id: Id, seq: int) -> None:
        unlocked()
        conn.execute("pragma foreign_keys = off")
        conn.execute(
            "delete from audit_event where project_id = ? and seq = ?", (str(project_id), seq)
        )
        conn.execute("pragma foreign_keys = on")

    def insert(event: AuditEvent) -> None:
        ledger.restore([event], [])

    def overwrite(event: AuditEvent) -> None:
        unlocked()
        conn.execute(
            "update audit_event set event_id = ?, type = ?, schema_version = ?, actor_id = ?,"
            " recorded_at = ?, payload = ?, payload_hash = ?, prev_hash = ?, event_hash = ?"
            " where project_id = ? and seq = ?",
            (
                str(event.event_id),
                event.type,
                event.schema_version,
                str(event.actor_id),
                event.recorded_at.isoformat(),
                canonical_json(event.payload).decode(),
                event.payload_hash,
                event.prev_hash,
                event.event_hash,
                str(event.project_id),
                event.seq,
            ),
        )

    try:
        yield Harness("sqlite", ledger, null_tx(), clock, _ids, delete, insert, overwrite)
    finally:
        ledger.close()


def postgres_harness(pg: Tx) -> Iterator[Harness]:
    clock = TickingClock(START)
    ledger = PostgresLedger(clock)

    def delete(project_id: Id, seq: int) -> None:
        # The table owner can switch the guard off, which is why checkpoints exist.
        pg.execute("alter table audit_event disable trigger user")
        pg.execute("delete from checkpoint where project_id = %s and seq >= %s", (project_id, seq))
        pg.execute("delete from audit_event where project_id = %s and seq = %s", (project_id, seq))
        pg.execute("alter table audit_event enable trigger user")

    def insert(event: AuditEvent) -> None:
        ledger._insert_event(pg, event)

    def overwrite(event: AuditEvent) -> None:
        pg.execute("alter table audit_event disable trigger user")
        pg.execute(
            "update audit_event set event_id = %s, type = %s, schema_version = %s,"
            " actor_id = %s, recorded_at = %s, payload = %s, payload_hash = %s, prev_hash = %s,"
            " event_hash = %s where project_id = %s and seq = %s",
            (
                event.event_id,
                event.type,
                event.schema_version,
                event.actor_id,
                event.recorded_at,
                Jsonb(event.payload),
                event.payload_hash,
                event.prev_hash,
                event.event_hash,
                event.project_id,
                event.seq,
            ),
        )
        pg.execute("alter table audit_event enable trigger user")

    yield Harness(
        "postgres", ledger, pg, clock, lambda: seed_project(pg), delete, insert, overwrite
    )
