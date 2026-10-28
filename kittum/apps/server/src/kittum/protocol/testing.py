"""An in-memory `ProtocolStore` for tests in any module. It follows the Postgres store's rules.

It ignores the transaction; pass `kittum.db.testing.null_tx()`. It cannot check that the approver
is a human actor, because it has no actor table, so it trusts the ID.
"""

from typing import TYPE_CHECKING

from kittum.core import Clock, Id, NotFoundError, SystemClock, new_id
from kittum.protocol.store import (
    StoredProtocol,
    check_publishable,
    mark_published,
    published_event,
)

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import Ledger
    from kittum.protocol.compiler import ProtocolVersion


class InMemoryProtocolStore:
    def __init__(self, ledger: Ledger, clock: Clock | None = None) -> None:
        """Pass a `kittum.ledger.testing.InMemoryLedger`, ideally on the same clock."""
        self._clock = clock or SystemClock()
        self.ledger = ledger
        self._versions: dict[Id, StoredProtocol] = {}

    def save_draft(self, tx: Tx, project_id: Id, version: ProtocolVersion) -> StoredProtocol:
        for stored in self._versions.values():
            if (
                stored.project_id == project_id
                and stored.version.content_hash == version.content_hash
            ):
                return stored
        parent = self.latest_published(tx, project_id)
        stored = StoredProtocol(
            id=new_id(),
            project_id=project_id,
            parent_id=parent.id if parent else None,
            status="draft",
            version=version,
            created_at=self._clock.now(),
        )
        self._versions[stored.id] = stored
        return stored

    def publish(
        self, tx: Tx, project_id: Id, version_id: Id, approved_by: Id, *, reason: str | None = None
    ) -> StoredProtocol:
        stored = self.get_version(tx, project_id, version_id)
        previous = self.latest_published(tx, project_id)
        check_publishable(stored, previous, reason)
        event = self.ledger.append(tx, published_event(stored, previous, approved_by, reason))
        published = mark_published(stored, event, approved_by, reason)
        self._versions[version_id] = published
        return published

    def get_version(self, tx: Tx, project_id: Id, version_id: Id) -> StoredProtocol:
        stored = self._versions.get(version_id)
        if stored is None or stored.project_id != project_id:
            raise NotFoundError(f"protocol version {version_id} not found in project {project_id}")
        return stored

    def list_versions(self, tx: Tx, project_id: Id) -> list[StoredProtocol]:
        return sorted(
            (s for s in self._versions.values() if s.project_id == project_id),
            key=lambda s: (s.created_at, s.id),
        )

    def latest_published(self, tx: Tx, project_id: Id) -> StoredProtocol | None:
        published = [
            s for s in self._versions.values() if s.project_id == project_id and s.published_at
        ]
        if not published:
            return None
        return max(published, key=lambda s: (s.published_at, s.id))
