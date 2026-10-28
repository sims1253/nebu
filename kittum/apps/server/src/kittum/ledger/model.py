"""The values the ledger reads and writes."""

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

from pydantic import StringConstraints

from kittum.core import Id, KittumError, Record, Sha256

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger.keys import SigningKey, VerifyKey

type SignatureHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]

type Problem = Literal[
    "wrong_project",
    "seq_gap",
    "payload_mismatch",
    "prev_hash_mismatch",
    "event_hash_mismatch",
    "untrusted_key",
    "bad_signature",
    "tail_cut",
    "rewritten",
]
"""Why a chain failed to verify. `tail_cut` and `rewritten` need a trusted checkpoint to detect."""


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


class Checkpoint(Record):
    """A signed statement that the chain of `project_id` had `head_hash` at `seq`.

    Copies live outside the database, so a checkpoint still convicts a chain that an administrator
    rewrote or cut, as long as the signing key was not theirs.
    """

    id: Id
    project_id: Id
    seq: int
    head_hash: Sha256
    key_id: Sha256
    signature: SignatureHex
    created_at: datetime
    tsa_token_hash: Sha256 | None = None


class VerifyReport(Record):
    project_id: Id
    checked_through_seq: int
    ok: bool
    first_bad_seq: int | None = None
    problem: Problem | None = None
    reason: str | None = None
    checkpoint_seq: int | None = None


type ChainCheck = Literal["seq", "payload_hash", "prev_hash", "event_hash", "checkpoint"]
"""Which recomputation an event or checkpoint failed in a `ChainAudit`."""


class Finding(Record):
    """One check that failed, with what the row stores and what recomputing it gives.

    `seq` is the event the check ran on; a checkpoint's finding names the seq it covers.
    """

    seq: int
    event_id: Id | None = None
    type: str | None = None
    check: ChainCheck
    problem: Problem
    stored: str | None = None
    expected: str | None = None
    reason: str


class CheckpointStatus(Record):
    checkpoint: Checkpoint
    signature: Literal["valid", "invalid", "unknown_key", "unchecked"]
    """`unknown_key`: signed by another key than the trusted one; `unchecked`: no trusted key."""
    head_matches: bool | None
    """Whether the stored event at its seq still has the signed hash; None if the event is gone."""


class ChainAudit(Record):
    """Every check on every event and checkpoint, not only the first failure (`verify_chain`).

    A tampered row shows up as the exact check it fails: a changed payload under its old hash
    fails `payload_hash`; a re-hashed payload fails `event_hash`; a fully re-hashed event fails
    the next event's `prev_hash`; a re-hashed tail fails the signed `checkpoint` that covers it;
    a deleted event leaves a `seq` gap.
    """

    project_id: Id
    events: int
    head_seq: int
    ok: bool
    verified_through_seq: int
    """The last seq before the first finding: the prefix of the chain that holds."""
    findings: tuple[Finding, ...]
    checkpoints: tuple[CheckpointStatus, ...]
    trusted_key_id: Sha256 | None


class LedgerIntegrityError(KittumError):
    """The chain failed verification, so the ledger refuses to sign or seal it."""

    def __init__(self, report: VerifyReport) -> None:
        super().__init__(f"seq {report.first_bad_seq}: {report.reason}")
        self.report = report


class Ledger(Protocol):
    """Appends run inside the caller's transaction (see `kittum.db`)."""

    def append(self, tx: Tx, draft: EventDraft) -> AuditEvent: ...

    def events(
        self, tx: Tx, project_id: Id, *, after_seq: int = 0, through_seq: int | None = None
    ) -> list[AuditEvent]: ...

    def verify(
        self,
        tx: Tx,
        project_id: Id,
        *,
        checkpoint: Checkpoint | None = None,
        key: VerifyKey | None = None,
    ) -> VerifyReport: ...

    def checkpoint(
        self, tx: Tx, project_id: Id, signer: SigningKey, *, seq: int | None = None
    ) -> Checkpoint: ...

    def checkpoints(self, tx: Tx, project_id: Id) -> list[Checkpoint]: ...
