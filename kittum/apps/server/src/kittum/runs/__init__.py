"""Commands, receipts, and durable jobs (DESIGN.md, "Runs and reproducibility" and "Store").

A write follows one path. A pure decider turns a command into events and jobs without I/O. One
transaction commits the events, the module's own rows, a receipt for the command, and the jobs.
Workers claim jobs with a lease, run them, and report back with a new command. A retried command
finds its receipt and changes nothing.

Owner: WP-02. Shipped now: the types and the queue interface. Still to build: the Postgres job
table with `SKIP LOCKED` leases and heartbeats, receipts, the worker loop, run manifests, and
`diff` between runs.
"""

from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from kittum.core import Id, Record, Sha256

type JobStatus = Literal["queued", "running", "succeeded", "failed", "dead"]


class Command(Record):
    command_id: Id
    project_id: Id
    type: str
    actor_id: Id
    payload: dict[str, Any]


class Receipt(Record):
    command_id: Id
    accepted_at: datetime
    event_seqs: tuple[int, ...]


class JobSpec(Record):
    """Work to do after a commit. `stage_key` is unique, so a job is never published twice."""

    project_id: Id
    kind: str
    stage_key: str
    payload: dict[str, Any]


class Job(Record):
    job_id: Id
    spec: JobSpec
    attempt: int
    status: JobStatus
    lease_until: datetime | None = None
    output_hash: Sha256 | None = None


class JobQueue(Protocol):
    def enqueue(self, spec: JobSpec) -> Job: ...

    def claim(self, kinds: tuple[str, ...], worker: str, lease: timedelta) -> Job | None: ...

    def heartbeat(self, job_id: Id, lease: timedelta) -> None: ...

    def complete(self, job_id: Id, output_hash: str) -> None: ...

    def fail(self, job_id: Id, error: str, *, retry_at: datetime | None) -> None: ...


__all__ = ["Command", "Job", "JobQueue", "JobSpec", "JobStatus", "Receipt"]
