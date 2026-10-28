"""Chain verification, shared by every store and the offline bundle check."""

from typing import TYPE_CHECKING

from kittum.ledger.chain import GENESIS_HASH, event_hash, payload_hash
from kittum.ledger.model import VerifyReport

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kittum.core import Id
    from kittum.ledger.keys import VerifyKey
    from kittum.ledger.model import AuditEvent, Checkpoint, Problem


def verify_chain(
    project_id: Id,
    events: Iterable[AuditEvent],
    *,
    checkpoint: Checkpoint | None = None,
    key: VerifyKey | None = None,
) -> VerifyReport:
    """Check `events`, in seq order, against the chain rules and optionally a trusted checkpoint.

    Without a checkpoint this catches any edit that leaves the chain inconsistent: a changed
    payload, a removed event, a reordered event. Someone who can write the table can still
    rewrite every hash from some point on, or drop the newest events. Only a checkpoint signed by
    `key` catches those, so pass one whenever a copy exists outside the database.
    """
    if checkpoint is not None and key is None:
        raise ValueError("verifying against a checkpoint needs the trusted key")

    def bad(seq: int, problem: Problem, reason: str) -> VerifyReport:
        return VerifyReport(
            project_id=project_id,
            checked_through_seq=seq - 1,
            ok=False,
            first_bad_seq=seq,
            problem=problem,
            reason=reason,
            checkpoint_seq=checkpoint.seq if checkpoint else None,
        )

    if checkpoint is not None and key is not None:
        if checkpoint.project_id != project_id:
            return bad(1, "wrong_project", "the checkpoint belongs to another project")
        if checkpoint.key_id != key.key_id:
            return bad(1, "untrusted_key", f"the checkpoint was signed by key {checkpoint.key_id}")
        if not key.verifies_checkpoint(checkpoint):
            return bad(1, "bad_signature", "the checkpoint signature does not verify")

    prev = GENESIS_HASH
    seq = 0
    hash_at_checkpoint: str | None = None
    for e in events:
        expected = seq + 1
        if e.project_id != project_id:
            return bad(expected, "wrong_project", f"event {e.event_id} belongs to another project")
        if e.seq != expected:
            return bad(expected, "seq_gap", f"expected seq {expected}, found {e.seq}")
        if payload_hash(e.payload) != e.payload_hash:
            return bad(expected, "payload_mismatch", "the payload does not match its hash")
        if e.prev_hash != prev:
            return bad(expected, "prev_hash_mismatch", "prev_hash does not match the event before")
        recomputed = event_hash(
            project_id=e.project_id,
            seq=e.seq,
            event_id=e.event_id,
            type=e.type,
            schema_version=e.schema_version,
            actor_id=e.actor_id,
            recorded_at=e.recorded_at,
            payload_hash=e.payload_hash,
            prev_hash=prev,
        )
        if e.event_hash != recomputed:
            return bad(expected, "event_hash_mismatch", "the event does not match its hash")
        prev = e.event_hash
        seq = expected
        if checkpoint is not None and seq == checkpoint.seq:
            hash_at_checkpoint = e.event_hash

    if checkpoint is not None:
        if seq < checkpoint.seq:
            return bad(
                seq + 1,
                "tail_cut",
                f"the chain ends at seq {seq}, but a checkpoint covers seq {checkpoint.seq}",
            )
        if hash_at_checkpoint != checkpoint.head_hash:
            return bad(
                checkpoint.seq,
                "rewritten",
                f"the chain at seq {checkpoint.seq} differs from the signed checkpoint",
            )
    return VerifyReport(
        project_id=project_id,
        checked_through_seq=seq,
        ok=True,
        checkpoint_seq=checkpoint.seq if checkpoint else None,
    )
