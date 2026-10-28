"""In-memory fakes for the job queue, receipts, and transactions.

They follow the Postgres rules (stage keys, leases, backoff, dead-lettering) but cannot roll back:
if `apply` raises after events were appended to an `InMemoryLedger`, those events stay.

    commands = Commands(InMemoryLedger(clock), InMemoryJobQueue(clock), InMemoryReceipts(), clock)
    worker = Worker(NullTransactions(), queue, {"judge": handle}, name="w1")
"""

import random
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from kittum.core import Clock, SystemClock, canonical_json, new_id
from kittum.runs.model import Job, LeaseLost, RetryPolicy, StageKeyConflict

if TYPE_CHECKING:
    from collections.abc import Generator
    from datetime import timedelta

    from kittum.core import Id
    from kittum.db import Tx
    from kittum.runs.model import JobSpec, Receipt


class _NoSql:
    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an in-memory fake should not run SQL")


class NullTransactions:
    """Opens transactions that run no SQL, for workers over the in-memory queue."""

    @contextmanager
    def transaction(self) -> Generator[Tx]:
        yield cast("Tx", _NoSql())


class InMemoryJobQueue:
    def __init__(
        self,
        clock: Clock | None = None,
        retry: RetryPolicy | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._retry = retry or RetryPolicy()
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._jobs: dict[Id, Job] = {}

    def jobs(self) -> list[Job]:
        return list(self._jobs.values())

    def enqueue(self, tx: Tx, spec: JobSpec) -> Job:
        with self._lock:
            for job in self._jobs.values():
                if job.spec.stage_key == spec.stage_key:
                    if canonical_json(job.spec) != canonical_json(spec):
                        raise StageKeyConflict(
                            f"stage {spec.stage_key!r} was published with other inputs"
                        )
                    return job
            job = Job(
                job_id=new_id(), spec=spec, attempt=0, status="queued", run_after=self._clock.now()
            )
            self._jobs[job.job_id] = job
            return job

    def claim(self, tx: Tx, kinds: tuple[str, ...], worker: str, lease: timedelta) -> Job | None:
        now = self._clock.now()
        with self._lock:
            due = sorted(
                (
                    j
                    for j in self._jobs.values()
                    if j.spec.kind in kinds
                    and (
                        (j.status == "queued" and j.run_after <= now)
                        or (j.status == "running" and j.lease_until and j.lease_until <= now)
                    )
                ),
                key=lambda j: (j.run_after, j.job_id),
            )
            for job in due:
                if job.status == "running" and job.attempt >= self._retry.max_attempts:
                    self._put(
                        job,
                        status="dead",
                        lease_until=None,
                        last_error=f"the lease of {job.lease_owner} expired on the last attempt",
                    )
                    continue
                return self._put(
                    job,
                    status="running",
                    attempt=job.attempt + 1,
                    lease_owner=worker,
                    lease_until=now + lease,
                )
            return None

    def heartbeat(self, tx: Tx, job: Job, lease: timedelta) -> Job:
        with self._lock:
            return self._put(self._leased(job), lease_until=self._clock.now() + lease)

    def complete(self, tx: Tx, job: Job, output_hash: str | None = None) -> Job:
        with self._lock:
            return self._put(
                self._leased(job),
                status="succeeded",
                lease_until=None,
                output_hash=output_hash,
                last_error=None,
            )

    def fail(self, tx: Tx, job: Job, error: str, *, retryable: bool = True) -> Job:
        with self._lock:
            current = self._leased(job)
            if not retryable:
                return self._put(current, status="failed", lease_until=None, last_error=error)
            if job.attempt >= self._retry.max_attempts:
                return self._put(current, status="dead", lease_until=None, last_error=error)
            return self._put(
                current,
                status="queued",
                lease_owner=None,
                lease_until=None,
                run_after=self._clock.now() + self._retry.delay(job.attempt, self._rng),
                last_error=error,
            )

    def get(self, tx: Tx, job_id: Id) -> Job:
        return self._jobs[job_id]

    def _leased(self, job: Job) -> Job:
        current = self._jobs.get(job.job_id)
        if (
            current is None
            or current.status != "running"
            or current.lease_owner != job.lease_owner
            or current.attempt != job.attempt
        ):
            raise LeaseLost(f"job {job.job_id} attempt {job.attempt} is no longer leased")
        return current

    def _put(self, job: Job, **changes: object) -> Job:
        updated = job.model_copy(update=changes)
        self._jobs[job.job_id] = updated
        return updated


class InMemoryReceipts:
    def __init__(self) -> None:
        self._receipts: dict[Id, Receipt] = {}

    def find(self, tx: Tx, command_id: Id) -> Receipt | None:
        return self._receipts.get(command_id)

    def save(self, tx: Tx, receipt: Receipt) -> None:
        if receipt.command_id in self._receipts:
            raise KeyError(f"receipt {receipt.command_id} exists")
        self._receipts[receipt.command_id] = receipt
