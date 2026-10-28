"""The values and seams of `kittum.runs`."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, Protocol

from kittum.core import Id, KittumError, Record, Sha256

if TYPE_CHECKING:
    import random
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from kittum.db import Tx
    from kittum.ledger import AuditEvent, EventDraft

type JobStatus = Literal["queued", "running", "succeeded", "failed", "dead"]
"""`failed` means a permanent error; `dead` means the retries ran out."""


class Command(Record):
    """A request to change state. Callers pick `command_id`, so a retry reuses it."""

    command_id: Id
    project_id: Id
    type: str
    actor_id: Id
    payload: dict[str, Any]


class Receipt(Record):
    command_id: Id
    project_id: Id
    type: str
    accepted_at: datetime
    event_seqs: tuple[int, ...]


class JobSpec(Record):
    """Work to do after a commit. `stage_key` is unique, so a job is never published twice."""

    project_id: Id
    kind: str
    stage_key: str
    payload: dict[str, Any]


class Job(Record):
    """A job as last read. A claimed job is also the lease: only its owner and attempt may
    heartbeat, complete, or fail it."""

    job_id: Id
    spec: JobSpec
    attempt: int
    status: JobStatus
    run_after: datetime
    lease_owner: str | None = None
    lease_until: datetime | None = None
    output_hash: Sha256 | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class Decision[T = None]:
    """What a decider returns: events for the ledger, jobs to run after commit, and the module's
    own changes, which `apply` writes."""

    events: tuple[EventDraft, ...] = ()
    jobs: tuple[JobSpec, ...] = ()
    changes: T | None = None


type Decider[S, T] = Callable[[Command, S], Decision[T]]
"""A pure function. It may raise `CommandRejected`; it may not do I/O."""


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter. The n-th failure waits between half and all of
    `base * 2**(n-1)`, capped at `cap`. After `max_attempts` attempts a job is dead."""

    max_attempts: int = 5
    base: timedelta = timedelta(seconds=5)
    cap: timedelta = timedelta(minutes=30)

    def delay(self, attempt: int, rng: random.Random) -> timedelta:
        ceiling = min(self.cap, self.base * 2 ** min(attempt - 1, 32))
        return ceiling * rng.uniform(0.5, 1.0)


class LeaseLost(KittumError):
    """The job's lease went to another worker, or the job is no longer running."""


class StageKeyConflict(KittumError):
    """A job with this stage key exists, with a different kind, project, or payload."""


class CommandRejected(KittumError):
    """A decider refused the command. Nothing is written, so the caller may retry."""


class CommandIdReused(KittumError):
    """A receipt exists for this command ID, but for another project or command type."""


class PermanentJobError(KittumError):
    """Raised by a job handler for an error that retrying cannot fix."""


class JobQueue(Protocol):
    def enqueue(self, tx: Tx, spec: JobSpec) -> Job:
        """Publish a job, or return the existing one with the same stage key."""
        ...

    def claim(self, tx: Tx, kinds: tuple[str, ...], worker: str, lease: timedelta) -> Job | None:
        """Lease the next due job, or a running one whose lease expired."""
        ...

    def heartbeat(self, tx: Tx, job: Job, lease: timedelta) -> Job: ...

    def complete(self, tx: Tx, job: Job, output_hash: str | None = None) -> Job: ...

    def fail(self, tx: Tx, job: Job, error: str, *, retryable: bool = True) -> Job: ...

    def get(self, tx: Tx, job_id: Id) -> Job: ...


class ReceiptStore(Protocol):
    def find(self, tx: Tx, command_id: Id) -> Receipt | None:
        """Return the receipt, holding off other callers with the same command ID until commit."""
        ...

    def save(self, tx: Tx, receipt: Receipt) -> None: ...


class Transactions(Protocol):
    """Something that opens transactions: `kittum.db.Database`, or a fake."""

    def transaction(self) -> AbstractContextManager[Tx]: ...


type Apply[T] = Callable[[Tx, Decision[T], tuple[AuditEvent, ...]], None]
