"""The worker loop: claim, run, heartbeat, and publish, with graceful shutdown."""

import logging
import signal
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from kittum.runs.model import LeaseLost, PermanentJobError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from kittum.db import Tx
    from kittum.runs.model import Job, JobQueue, Transactions

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobOutput:
    """What a handler returns.

    `output_hash` names the artifact the job stored (store it before returning). `publish` runs
    in the same transaction that completes the job, so a worker that lost its lease publishes
    nothing; feed results back there, for example with `Commands.execute`.
    """

    output_hash: str | None = None
    publish: Callable[[Tx], object] | None = None


@dataclass(frozen=True)
class JobContext:
    job: Job
    lease_lost: threading.Event = field(default_factory=threading.Event)
    """Set when another worker took the job over. Long handlers should check it and give up."""
    stopping: threading.Event = field(default_factory=threading.Event)
    """Set when the worker is shutting down. The worker still waits for the handler."""


type Handler = Callable[[Job, JobContext], JobOutput]


class Worker:
    def __init__(
        self,
        db: Transactions,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        name: str,
        lease: timedelta = timedelta(minutes=5),
        heartbeat_every: timedelta | None = None,
        poll_interval: timedelta = timedelta(seconds=1),
    ) -> None:
        if not handlers:
            raise ValueError("a worker needs at least one handler")
        self.name = name
        self._db = db
        self._queue = queue
        self._handlers = dict(handlers)
        self._kinds = tuple(sorted(handlers))
        self._lease = lease
        self._heartbeat_every = heartbeat_every or lease / 3
        self._poll = poll_interval.total_seconds()
        self._stop = threading.Event()

    def stop(self) -> None:
        """Ask the loop to exit after the current job. Safe from any thread or signal handler."""
        self._stop.set()

    def stop_on_signals(self) -> None:
        """Stop gracefully on SIGTERM and SIGINT. Call from the main thread of an entry point."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop())

    def run(self) -> None:
        """Run jobs until `stop()`. A job in progress always finishes or fails cleanly first."""
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker %s: claiming or recording a job failed", self.name)
                worked = False
            if not worked:
                self._stop.wait(self._poll)

    def run_once(self) -> bool:
        """Claim and run one job. Returns False if nothing was due."""
        with self._db.transaction() as tx:
            job = self._queue.claim(tx, self._kinds, self.name, self._lease)
        if job is None:
            return False

        ctx = JobContext(job=job, stopping=self._stop)
        done = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat, args=(job, ctx, done), name=f"{self.name}-heartbeat"
        )
        beat.start()
        output: JobOutput | None = None
        error: str | None = None
        retryable = True
        try:
            output = self._handlers[job.spec.kind](job, ctx)
        except PermanentJobError as e:
            error, retryable = f"{type(e).__name__}: {e}", False
        except Exception as e:
            log.exception("worker %s: job %s failed", self.name, job.job_id)
            error = f"{type(e).__name__}: {e}"
        finally:
            done.set()
            beat.join()

        try:
            with self._db.transaction() as tx:
                if output is None:
                    self._queue.fail(tx, job, error or "no output", retryable=retryable)
                else:
                    self._queue.complete(tx, job, output.output_hash)
                    if output.publish is not None:
                        output.publish(tx)
        except LeaseLost:
            log.warning(
                "worker %s: lost the lease on job %s; output dropped", self.name, job.job_id
            )
        return True

    def _heartbeat(self, job: Job, ctx: JobContext, done: threading.Event) -> None:
        while not done.wait(self._heartbeat_every.total_seconds()):
            try:
                with self._db.transaction() as tx:
                    self._queue.heartbeat(tx, job, self._lease)
            except LeaseLost:
                ctx.lease_lost.set()
                return
            except Exception:
                log.exception("worker %s: heartbeat for job %s failed", self.name, job.job_id)
