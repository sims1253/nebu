"""The job queue and command receipts on the foundation tables `job` and `command_receipt`."""

import random
from datetime import UTC
from typing import TYPE_CHECKING, Any, LiteralString

from psycopg.types.json import Jsonb

from kittum.core import Clock, SystemClock, canonical_json, new_id
from kittum.runs.model import Job, JobSpec, LeaseLost, Receipt, RetryPolicy, StageKeyConflict

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from kittum.core import Id
    from kittum.db import Tx

# The first keys of the two-key advisory locks on a command ID and on a project's commands; the
# ledger uses another.
LOCK_NAMESPACE = 0x6B72756E  # "krun"
PROJECT_LOCK_NAMESPACE = 0x6B70726A  # "kprj"

_LEASED = "id = %s and status = 'running' and lease_owner = %s and attempt = %s"


def _utc(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value else None


def _job(row: dict[str, Any]) -> Job:
    return Job(
        job_id=row["id"],
        spec=JobSpec(
            project_id=row["project_id"],
            kind=row["kind"],
            stage_key=row["stage_key"],
            payload=row["payload"],
        ),
        attempt=row["attempt"],
        status=row["status"],
        run_after=row["run_after"].astimezone(UTC),
        lease_owner=row["lease_owner"],
        lease_until=_utc(row["lease_until"]),
        output_hash=row["output_hash"],
        last_error=row["last_error"],
    )


class PostgresJobQueue:
    """Claims use `for update skip locked`, so concurrent workers never wait on or share a job.

    Each call runs in the caller's transaction. A worker should commit a claim at once, run the
    job outside any transaction, and then complete it in a new one (see `Worker`).
    """

    def __init__(
        self,
        clock: Clock | None = None,
        retry: RetryPolicy | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._retry = retry or RetryPolicy()
        self._rng = rng or random.Random()

    def enqueue(self, tx: Tx, spec: JobSpec) -> Job:
        row = tx.execute(
            "insert into job (id, project_id, kind, stage_key, payload, run_after)"
            " values (%s, %s, %s, %s, %s, %s)"
            " on conflict (stage_key) do nothing returning *",
            (
                new_id(),
                spec.project_id,
                spec.kind,
                spec.stage_key,
                Jsonb(spec.payload),
                self._clock.now(),
            ),
        ).fetchone()
        if row is not None:
            return _job(row)
        existing = self.get_by_stage_key(tx, spec.stage_key)
        if canonical_json(existing.spec) != canonical_json(spec):
            raise StageKeyConflict(f"stage {spec.stage_key!r} was published with other inputs")
        return existing

    def claim(self, tx: Tx, kinds: tuple[str, ...], worker: str, lease: timedelta) -> Job | None:
        now = self._clock.now()
        while True:
            row = tx.execute(
                "select id, status, attempt, lease_owner from job"
                " where kind = any(%s) and ((status = 'queued' and run_after <= %s)"
                " or (status = 'running' and lease_until <= %s))"
                " order by run_after, id limit 1 for update skip locked",
                (list(kinds), now, now),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == "running" and row["attempt"] >= self._retry.max_attempts:
                tx.execute(
                    "update job set status = 'dead', lease_until = null, last_error = %s"
                    " where id = %s",
                    (f"the lease of {row['lease_owner']} expired on the last attempt", row["id"]),
                )
                continue
            claimed = tx.execute(
                "update job set status = 'running', attempt = attempt + 1, lease_owner = %s,"
                " lease_until = %s where id = %s returning *",
                (worker, now + lease, row["id"]),
            ).fetchone()
            assert claimed is not None
            return _job(claimed)

    def heartbeat(self, tx: Tx, job: Job, lease: timedelta) -> Job:
        return self._leased(tx, job, "lease_until = %s", (self._clock.now() + lease,))

    def fence(self, tx: Tx, job: Job) -> None:
        row = tx.execute(
            f"select id from job where {_LEASED} for update",
            (
                job.job_id,
                job.lease_owner,
                job.attempt,
            ),
        ).fetchone()
        if row is None:
            raise LeaseLost(f"job {job.job_id} attempt {job.attempt} is no longer leased")

    def complete(self, tx: Tx, job: Job, output_hash: str | None = None) -> Job:
        return self._leased(
            tx,
            job,
            "status = 'succeeded', lease_until = null, output_hash = %s, last_error = null",
            (output_hash,),
        )

    def fail(self, tx: Tx, job: Job, error: str, *, retryable: bool = True) -> Job:
        if not retryable:
            return self._leased(
                tx, job, "status = 'failed', lease_until = null, last_error = %s", (error,)
            )
        if job.attempt >= self._retry.max_attempts:
            return self._leased(
                tx, job, "status = 'dead', lease_until = null, last_error = %s", (error,)
            )
        run_after = self._clock.now() + self._retry.delay(job.attempt, self._rng)
        return self._leased(
            tx,
            job,
            "status = 'queued', lease_owner = null, lease_until = null, run_after = %s,"
            " last_error = %s",
            (run_after, error),
        )

    def get(self, tx: Tx, job_id: Id) -> Job:
        row = tx.execute("select * from job where id = %s", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job(row)

    def get_by_stage_key(self, tx: Tx, stage_key: str) -> Job:
        row = tx.execute("select * from job where stage_key = %s", (stage_key,)).fetchone()
        if row is None:
            raise KeyError(stage_key)
        return _job(row)

    def _leased(self, tx: Tx, job: Job, assignments: LiteralString, params: tuple[Any, ...]) -> Job:
        row = tx.execute(
            f"update job set {assignments} where {_LEASED} returning *",
            (*params, job.job_id, job.lease_owner, job.attempt),
        ).fetchone()
        if row is None:
            raise LeaseLost(f"job {job.job_id} attempt {job.attempt} is no longer leased")
        return _job(row)


class PostgresReceipts:
    def lock_project(self, tx: Tx, project_id: Id) -> None:
        tx.execute(
            "select pg_advisory_xact_lock(%s, hashtext(%s::text))",
            (PROJECT_LOCK_NAMESPACE, project_id),
        )

    def find(self, tx: Tx, command_id: Id) -> Receipt | None:
        tx.execute(
            "select pg_advisory_xact_lock(%s, hashtext(%s::text))", (LOCK_NAMESPACE, command_id)
        )
        row = tx.execute(
            "select command_id, project_id, type, accepted_at, first_seq, last_seq"
            " from command_receipt where command_id = %s",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        seqs = () if row["first_seq"] is None else range(row["first_seq"], row["last_seq"] + 1)
        return Receipt(
            command_id=row["command_id"],
            project_id=row["project_id"],
            type=row["type"],
            accepted_at=row["accepted_at"].astimezone(UTC),
            event_seqs=tuple(seqs),
        )

    def save(self, tx: Tx, receipt: Receipt) -> None:
        seqs = receipt.event_seqs
        if seqs and list(seqs) != list(range(seqs[0], seqs[-1] + 1)):
            raise ValueError("a command's events must be contiguous in the ledger")
        tx.execute(
            "insert into command_receipt"
            " (command_id, project_id, type, accepted_at, first_seq, last_seq)"
            " values (%s, %s, %s, %s, %s, %s)",
            (
                receipt.command_id,
                receipt.project_id,
                receipt.type,
                receipt.accepted_at,
                seqs[0] if seqs else None,
                seqs[-1] if seqs else None,
            ),
        )
