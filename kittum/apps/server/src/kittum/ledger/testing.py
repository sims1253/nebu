"""An in-memory ledger for tests in any module. It follows the real chain rules.

It ignores the transaction; pass `kittum.db.testing.null_tx()`. `seed_project` creates the
foundation rows that a Postgres `audit_event` needs.
"""

import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

from kittum.core import new_id
from kittum.ledger.store import ChainStore

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from kittum.core import Clock, Id
    from kittum.db import Tx
    from kittum.ledger.model import AuditEvent, Checkpoint


class InMemoryLedger(ChainStore):
    def __init__(self, clock: Clock | None = None) -> None:
        super().__init__(clock)
        self._lock = threading.RLock()
        self._events: dict[Id, list[AuditEvent]] = {}
        self._checkpoints: dict[Id, list[Checkpoint]] = {}

    @contextmanager
    def _atomic(self, tx: Tx) -> Generator[None]:
        with self._lock:
            yield

    def _lock_head(self, tx: Tx, project_id: Id) -> AuditEvent | None:
        chain = self._events.get(project_id)
        return chain[-1] if chain else None

    def _insert_event(self, tx: Tx, event: AuditEvent) -> None:
        self._events.setdefault(event.project_id, []).append(event)

    def _iter_events(
        self, tx: Tx, project_id: Id, after_seq: int, through_seq: int | None
    ) -> Iterator[AuditEvent]:
        for e in list(self._events.get(project_id, [])):
            if e.seq > after_seq and (through_seq is None or e.seq <= through_seq):
                yield e

    def _insert_checkpoint(self, tx: Tx, checkpoint: Checkpoint) -> None:
        self._checkpoints.setdefault(checkpoint.project_id, []).append(checkpoint)

    def checkpoints(self, tx: Tx, project_id: Id) -> list[Checkpoint]:
        return sorted(self._checkpoints.get(project_id, []), key=lambda c: c.seq)


def seed_project(tx: Tx, *, name: str = "test project") -> tuple[Id, Id]:
    """Insert a project and a system actor; return their IDs."""
    project_id, actor_id = new_id(), new_id()
    tx.execute(
        "insert into project (id, name, policy_version) values (%s, %s, 'test')",
        (project_id, name),
    )
    tx.execute(
        "insert into actor (id, identity_ref, kind) values (%s, %s, 'system')",
        (actor_id, f"test:{actor_id}"),
    )
    return project_id, actor_id
