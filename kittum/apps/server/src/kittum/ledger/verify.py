"""Chain verification, shared by every store and the offline bundle check."""

from typing import TYPE_CHECKING

from kittum.ledger.chain import GENESIS_HASH, event_hash, payload_hash
from kittum.ledger.model import ChainAudit, CheckpointStatus, Finding, VerifyReport

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kittum.core import Id
    from kittum.ledger.keys import VerifyKey
    from kittum.ledger.model import AuditEvent, Check, Checkpoint, Problem


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


def audit_chain(
    project_id: Id,
    events: Iterable[AuditEvent],
    checkpoints: Iterable[Checkpoint] = (),
    *,
    key: VerifyKey | None = None,
) -> ChainAudit:
    """Run every check on every event and checkpoint, and report each one that fails.

    `verify_chain` stops at the first bad event, which is all a gate needs. An assessor needs the
    whole picture: which rows were touched and how. Each event is checked on its own stored
    fields (its payload against its payload hash, its fields against its event hash) and against
    the event stored before it (seq and prev hash), so one edited row is named once, with the
    check it fails, and the rows after it still verify. Checkpoints are checked against `key`
    when it is given, and against the stored event at their seq.
    """
    findings: list[Finding] = []

    def found(
        e: AuditEvent,
        check: Check,
        problem: Problem,
        reason: str,
        *,
        stored: str | None = None,
        expected: str | None = None,
    ) -> None:
        findings.append(
            Finding(
                seq=e.seq,
                event_id=e.event_id,
                type=e.type,
                check=check,
                problem=problem,
                stored=stored,
                expected=expected,
                reason=reason,
            )
        )

    hashes: dict[int, str] = {}
    prev: str | None = GENESIS_HASH
    expected = 1
    count = 0
    last = 0
    for e in events:
        count += 1
        last = e.seq

        if e.project_id != project_id:
            found(e, "seq", "wrong_project", f"the event belongs to project {e.project_id}")
        if e.seq != expected:
            missing = (
                f"seq {expected} is missing"
                if e.seq == expected + 1
                else f"seqs {expected} to {e.seq - 1} are missing"
            )
            findings.append(
                Finding(
                    seq=expected,
                    check="seq",
                    problem="seq_gap",
                    stored=str(e.seq),
                    expected=str(expected),
                    reason=f"{missing}: the next stored event is seq {e.seq}",
                )
            )
            prev = None  # the event this one links to is gone; its link cannot be checked
        recomputed_payload = payload_hash(e.payload)
        if recomputed_payload != e.payload_hash:
            found(
                e,
                "payload_hash",
                "payload_mismatch",
                "the payload was changed after it was recorded: it no longer matches its hash",
                stored=e.payload_hash,
                expected=recomputed_payload,
            )
        if prev is not None and e.prev_hash != prev:
            found(
                e,
                "prev_hash",
                "prev_hash_mismatch",
                f"the link to seq {e.seq - 1} is broken: prev_hash is not the hash stored there",
                stored=e.prev_hash,
                expected=prev,
            )
        recomputed_event = event_hash(
            project_id=e.project_id,
            seq=e.seq,
            event_id=e.event_id,
            type=e.type,
            schema_version=e.schema_version,
            actor_id=e.actor_id,
            recorded_at=e.recorded_at,
            payload_hash=e.payload_hash,
            prev_hash=e.prev_hash,
        )
        if recomputed_event != e.event_hash:
            found(
                e,
                "event_hash",
                "event_hash_mismatch",
                "a hashed field (payload hash, type, actor, time, seq, or prev hash) was changed:"
                " the event no longer matches its hash",
                stored=e.event_hash,
                expected=recomputed_event,
            )
        hashes[e.seq] = e.event_hash
        prev = e.event_hash
        expected = e.seq + 1

    statuses: list[CheckpointStatus] = []
    for c in checkpoints:
        if key is None:
            signature = "unchecked"
        elif c.key_id != key.key_id:
            signature = "unknown_key"
        else:
            signature = "valid" if key.verifies_checkpoint(c) else "invalid"
        stored = hashes.get(c.seq)
        statuses.append(
            CheckpointStatus(
                checkpoint=c,
                signature=signature,  # type: ignore[arg-type]
                head_matches=None if stored is None else stored == c.head_hash,
            )
        )
        if c.project_id != project_id:
            findings.append(
                Finding(
                    seq=c.seq,
                    check="checkpoint",
                    problem="wrong_project",
                    reason=f"checkpoint {c.id} belongs to project {c.project_id}",
                )
            )
        if signature == "invalid":
            findings.append(
                Finding(
                    seq=c.seq,
                    check="checkpoint",
                    problem="bad_signature",
                    stored=c.signature,
                    reason=f"the signature of checkpoint {c.id} does not verify with key {c.key_id}",
                )
            )
        if c.seq > last:
            findings.append(
                Finding(
                    seq=c.seq,
                    check="checkpoint",
                    problem="tail_cut",
                    expected=c.head_hash,
                    reason=f"the chain ends at seq {last}, but checkpoint {c.id} covers seq {c.seq}",
                )
            )
        elif stored is not None and stored != c.head_hash:
            findings.append(
                Finding(
                    seq=c.seq,
                    check="checkpoint",
                    problem="rewritten",
                    stored=stored,
                    expected=c.head_hash,
                    reason=f"the event at seq {c.seq} is not the one checkpoint {c.id} signed:"
                    " the chain was rewritten up to here",
                )
            )

    findings.sort(key=lambda f: (f.seq, f.check != "seq", f.check == "checkpoint"))
    first_bad = min((f.seq for f in findings), default=None)
    return ChainAudit(
        project_id=project_id,
        events=count,
        head_seq=last,
        ok=not findings,
        verified_through_seq=last if first_bad is None else first_bad - 1,
        findings=tuple(findings),
        checkpoints=tuple(statuses),
        trusted_key_id=key.key_id if key else None,
    )
