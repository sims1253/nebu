"""Commands, receipts, and durable jobs (DESIGN.md, "Runs and reproducibility" and "Store").

A write follows one path. A pure decider turns a command into events and jobs without I/O.
`Commands.execute` commits the events, the module's own rows, a receipt for the command, and the
jobs in the caller's transaction. A retried command finds its receipt and changes nothing.

`PostgresJobQueue` keeps jobs in the `job` table. Workers claim them with `skip locked` and a
lease, heartbeat while they run, and complete them in a transaction that also publishes their
result, so a worker that lost its lease publishes nothing. Failures retry with exponential
backoff and jitter until `RetryPolicy.max_attempts`, then the job is dead. A unique `stage_key`
keeps a stage from being published twice. `Worker` runs the loop and stops gracefully.

Fakes for tests in other modules are in `kittum.runs.testing`.

Still to build: run manifests, cache keys, and `diff` between runs.
"""

from kittum.runs.commands import Commands
from kittum.runs.model import (
    Apply,
    Command,
    CommandIdReused,
    CommandRejected,
    Decider,
    Decision,
    Job,
    JobQueue,
    JobSpec,
    JobStatus,
    LeaseLost,
    PermanentJobError,
    Receipt,
    ReceiptStore,
    RetryPolicy,
    StageKeyConflict,
    Transactions,
)
from kittum.runs.postgres import PostgresJobQueue, PostgresReceipts
from kittum.runs.worker import Handler, JobContext, JobOutput, Worker

__all__ = [
    "Apply",
    "Command",
    "CommandIdReused",
    "CommandRejected",
    "Commands",
    "Decider",
    "Decision",
    "Handler",
    "Job",
    "JobContext",
    "JobOutput",
    "JobQueue",
    "JobSpec",
    "JobStatus",
    "LeaseLost",
    "PermanentJobError",
    "PostgresJobQueue",
    "PostgresReceipts",
    "Receipt",
    "ReceiptStore",
    "RetryPolicy",
    "StageKeyConflict",
    "Transactions",
    "Worker",
]
