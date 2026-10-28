"""What every ledger store shares: placing events in the chain, verifying, and checkpointing.

A store only says how to lock a project's head, and how to read and write rows. The chain rules
live here once, so the Postgres store, the SQLite store, and the in-memory fake cannot drift.
"""

from abc import ABC, abstractmethod
from datetime import UTC
from typing import TYPE_CHECKING

from kittum.core import Clock, Id, SystemClock, new_id
from kittum.ledger.chain import GENESIS_HASH, event_hash, payload_hash
from kittum.ledger.model import AuditEvent, LedgerIntegrityError
from kittum.ledger.verify import verify_chain

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager

    from kittum.db import Tx
    from kittum.ledger.keys import SigningKey, VerifyKey
    from kittum.ledger.model import Checkpoint, EventDraft, VerifyReport


class ChainStore(ABC):
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()

    # What a store provides.

    @abstractmethod
    def _atomic(self, tx: Tx) -> AbstractContextManager[None]:
        """Make a head read and the writes that follow it one unit."""

    @abstractmethod
    def _lock_head(self, tx: Tx, project_id: Id) -> AuditEvent | None:
        """Return the newest event, holding off other appends to the project until commit."""

    @abstractmethod
    def _insert_event(self, tx: Tx, event: AuditEvent) -> None: ...

    @abstractmethod
    def _iter_events(
        self, tx: Tx, project_id: Id, after_seq: int, through_seq: int | None
    ) -> Iterator[AuditEvent]: ...

    @abstractmethod
    def _insert_checkpoint(self, tx: Tx, checkpoint: Checkpoint) -> None: ...

    @abstractmethod
    def checkpoints(self, tx: Tx, project_id: Id) -> list[Checkpoint]:
        """Every stored checkpoint for the project, oldest first."""

    # The ledger interface.

    def append(self, tx: Tx, draft: EventDraft) -> AuditEvent:
        with self._atomic(tx):
            head = self._lock_head(tx, draft.project_id)
            seq = head.seq + 1 if head else 1
            prev = head.event_hash if head else GENESIS_HASH
            event_id = new_id()
            # One time zone, so the hashed ISO string survives a round trip through any store.
            recorded_at = self._clock.now().astimezone(UTC)
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
            self._insert_event(tx, event)
            return event

    def events(
        self, tx: Tx, project_id: Id, *, after_seq: int = 0, through_seq: int | None = None
    ) -> list[AuditEvent]:
        return list(self._iter_events(tx, project_id, after_seq, through_seq))

    def verify(
        self,
        tx: Tx,
        project_id: Id,
        *,
        checkpoint: Checkpoint | None = None,
        key: VerifyKey | None = None,
    ) -> VerifyReport:
        """Verify the whole chain, and against `checkpoint` if given (see `verify_chain`).

        Take the checkpoint from a copy outside the database when there is one. One read from
        `checkpoints()` still proves the chain against its signature, but not that no newer
        checkpoint was deleted.
        """
        events = self._iter_events(tx, project_id, 0, None)
        return verify_chain(project_id, events, checkpoint=checkpoint, key=key)

    def checkpoint(
        self, tx: Tx, project_id: Id, signer: SigningKey, *, seq: int | None = None
    ) -> Checkpoint:
        """Verify the chain through `seq` (default: the head), sign its hash, and store that.

        Refuses to sign a chain that does not verify, or that contradicts an earlier checkpoint
        from the same key, so a checkpoint never launders tampering, even a fully re-hashed one.
        """
        with self._atomic(tx):
            head = self._lock_head(tx, project_id)
            if head is None:
                raise ValueError("the project has no events to checkpoint")
            seq = head.seq if seq is None else seq
            if not 1 <= seq <= head.seq:
                raise ValueError(f"seq {seq} is outside the chain (1 to {head.seq})")
            own = [c for c in self.checkpoints(tx, project_id) if c.key_id == signer.key_id]
            through = max([seq, *(c.seq for c in own)])
            events = self.events(tx, project_id, through_seq=through)
            report = verify_chain(project_id, events)
            if not report.ok or report.checked_through_seq < seq:
                raise LedgerIntegrityError(report)
            hashes = {e.seq: e.event_hash for e in events}
            for earlier in own:
                holds = hashes.get(earlier.seq) == earlier.head_hash
                if not holds or not signer.verify_key.verifies_checkpoint(earlier):
                    raise LedgerIntegrityError(
                        verify_chain(project_id, events, checkpoint=earlier, key=signer.verify_key)
                    )
            checkpoint = signer.sign_checkpoint(
                id=new_id(),
                project_id=project_id,
                seq=seq,
                head_hash=events[seq - 1].event_hash,
                created_at=self._clock.now().astimezone(UTC),
            )
            self._insert_checkpoint(tx, checkpoint)
            return checkpoint
