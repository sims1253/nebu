"""Workers against the real queue: concurrency, crashes, heartbeats, and shutdown."""

import logging
import threading
import time
from collections import Counter
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from kittum.core import FixedClock, new_id, sha256_hex
from kittum.db.testing import null_tx
from kittum.ledger import EventDraft, PostgresLedger
from kittum.ledger.testing import seed_project
from kittum.runs import (
    JobOutput,
    JobSpec,
    PermanentJobError,
    PostgresJobQueue,
    Worker,
)
from kittum.runs.testing import InMemoryJobQueue, NullTransactions
from tests.runs.conftest import START

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.core import Id
    from kittum.db import Database, Tx
    from kittum.runs import Handler, Job, JobContext

FAST = timedelta(milliseconds=10)


def _wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.01)


def _enqueue(db: Database, queue: PostgresJobQueue, kind: str, n: int) -> Id:
    with db.transaction() as tx:
        project, _ = seed_project(tx)
        for i in range(n):
            queue.enqueue(
                tx,
                JobSpec(project_id=project, kind=kind, stage_key=f"{kind}:{i}", payload={"i": i}),
            )
    return project


@pytest.mark.postgres
def test_two_workers_never_run_the_same_job(database: Database) -> None:
    kind = f"test.{new_id()}"
    queue = PostgresJobQueue()
    _enqueue(database, queue, kind, 40)

    lock = threading.Lock()
    runs: Counter[Id] = Counter()
    by_worker: Counter[str] = Counter()
    in_flight: set[Id] = set()
    overlaps: list[Id] = []
    # Each worker's first job waits for the other's, so both hold a claim at the same moment.
    both_busy = threading.Barrier(2, timeout=5)

    def handle(job: Job, _ctx: JobContext) -> JobOutput:
        assert job.lease_owner
        with lock:
            first = by_worker[job.lease_owner] == 0
            if job.job_id in in_flight:
                overlaps.append(job.job_id)
            in_flight.add(job.job_id)
            runs[job.job_id] += 1
            by_worker[job.lease_owner] += 1
        if first:
            both_busy.wait()
        time.sleep(0.002)
        with lock:
            in_flight.discard(job.job_id)
        return JobOutput(output_hash=sha256_hex(str(job.spec.payload["i"]).encode()))

    workers = [
        Worker(database, queue, {kind: handle}, name=name, poll_interval=FAST)
        for name in ("worker-a", "worker-b")
    ]
    threads = [threading.Thread(target=w.run) for w in workers]
    for t in threads:
        t.start()

    def all_done() -> bool:
        with database.transaction() as tx:
            row = tx.execute(
                "select count(*) as n from job where kind = %s and status = 'succeeded'", (kind,)
            ).fetchone()
        return row is not None and row["n"] == 40

    try:
        _wait_until(all_done)
    finally:
        for w in workers:
            w.stop()
        for t in threads:
            t.join(timeout=5)

    assert overlaps == []
    assert len(runs) == 40 and set(runs.values()) == {1}
    assert set(by_worker) == {"worker-a", "worker-b"}
    with database.transaction() as tx:
        rows = tx.execute(
            "select payload, attempt, output_hash from job where kind = %s", (kind,)
        ).fetchall()
    assert {r["attempt"] for r in rows} == {1}
    assert all(r["output_hash"] == sha256_hex(str(r["payload"]["i"]).encode()) for r in rows)


@pytest.mark.postgres
def test_a_crashed_workers_job_is_reclaimed_after_its_lease_and_published_once(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FixedClock(START)
    kind = f"test.{new_id()}"
    queue = PostgresJobQueue(clock)
    ledger = PostgresLedger(clock)
    with database.transaction() as tx:
        project, actor = seed_project(tx)
        queue.enqueue(tx, JobSpec(project_id=project, kind=kind, stage_key=kind, payload={}))

    hung = threading.Event()
    release = threading.Event()

    def handler_for(name: str, *, hang: bool) -> Handler:
        def handle(job: Job, _ctx: JobContext) -> JobOutput:
            if hang:  # The process stalls: no heartbeats, no progress.
                hung.set()
                release.wait(5)

            def publish(tx: Tx) -> None:
                ledger.append(
                    tx,
                    EventDraft(
                        project_id=project,
                        type="test.judged",
                        schema_version=1,
                        actor_id=actor,
                        payload={"by": name, "attempt": job.attempt},
                    ),
                )

            return JobOutput(output_hash="cd" * 32, publish=publish)

        return handle

    lease = timedelta(minutes=5)
    never = timedelta(hours=1)
    a = Worker(
        database, queue, {kind: handler_for("a", hang=True)}, name="a", lease=lease,
        heartbeat_every=never,
    )  # fmt: skip
    b = Worker(database, queue, {kind: handler_for("b", hang=False)}, name="b", lease=lease)
    zombie = threading.Thread(target=a.run_once)
    zombie.start()
    assert hung.wait(5)

    assert b.run_once() is False  # A's lease still holds.
    clock.advance(lease)
    with caplog.at_level(logging.WARNING):
        assert b.run_once() is True
        release.set()  # A wakes up and tries to publish its stale result.
        zombie.join(timeout=5)

    assert "lost the lease" in caplog.text
    with database.transaction() as tx:
        job = queue.get_by_stage_key(tx, kind)
        assert (job.status, job.attempt, job.lease_owner) == ("succeeded", 2, "b")
        published = [e.payload for e in ledger.events(tx, project) if e.type == "test.judged"]
    assert published == [{"by": "b", "attempt": 2}]


def test_heartbeats_keep_a_long_job_with_its_worker() -> None:
    queue = InMemoryJobQueue()
    project = new_id()
    queue.enqueue(
        null_tx(), JobSpec(project_id=project, kind="slow", stage_key="slow:1", payload={})
    )
    stolen: list[Job | None] = []

    def handle(_job: Job, ctx: JobContext) -> JobOutput:
        # Three leases long; another worker keeps trying to take the job meanwhile.
        for _ in range(9):
            time.sleep(0.1)
            stolen.append(queue.claim(null_tx(), ("slow",), "thief", timedelta(seconds=0.3)))
        assert not ctx.lease_lost.is_set()
        return JobOutput()

    worker = Worker(
        NullTransactions(), queue, {"slow": handle}, name="w", lease=timedelta(seconds=0.3),
        heartbeat_every=timedelta(seconds=0.05),
    )  # fmt: skip
    assert worker.run_once()
    assert stolen == [None] * 9
    assert [(j.status, j.attempt) for j in queue.jobs()] == [("succeeded", 1)]


def test_handler_errors_retry_but_permanent_errors_do_not() -> None:
    clock = FixedClock(START)
    queue = InMemoryJobQueue(clock)
    project = new_id()
    for kind in ("flaky", "broken"):
        queue.enqueue(null_tx(), JobSpec(project_id=project, kind=kind, stage_key=kind, payload={}))

    def flaky(_job: Job, _ctx: JobContext) -> JobOutput:
        raise TimeoutError("GROBID did not answer")

    def broken(_job: Job, _ctx: JobContext) -> JobOutput:
        raise PermanentJobError("the PDF is encrypted")

    worker = Worker(NullTransactions(), queue, {"flaky": flaky, "broken": broken}, name="w")
    assert worker.run_once() and worker.run_once()
    assert not worker.run_once()

    outcome = {j.spec.kind: (j.status, j.last_error) for j in queue.jobs()}
    assert outcome == {
        "flaky": ("queued", "TimeoutError: GROBID did not answer"),
        "broken": ("failed", "PermanentJobError: the PDF is encrypted"),
    }
    flaky_job = next(j for j in queue.jobs() if j.spec.kind == "flaky")
    assert flaky_job.run_after > clock.now()


def test_stop_lets_the_running_job_finish_and_claims_nothing_more() -> None:
    queue = InMemoryJobQueue()
    project = new_id()
    for i in range(2):
        queue.enqueue(
            null_tx(), JobSpec(project_id=project, kind="parse", stage_key=f"p{i}", payload={})
        )
    started = threading.Event()
    release = threading.Event()

    def handle(_job: Job, ctx: JobContext) -> JobOutput:
        started.set()
        release.wait(5)
        assert ctx.stopping.is_set()
        return JobOutput(output_hash="ef" * 32)

    worker = Worker(NullTransactions(), queue, {"parse": handle}, name="w", poll_interval=FAST)
    loop = threading.Thread(target=worker.run)
    loop.start()
    assert started.wait(5)
    worker.stop()  # as on SIGTERM
    release.set()
    loop.join(timeout=5)

    assert not loop.is_alive()
    assert sorted(j.status for j in queue.jobs()) == ["queued", "succeeded"]
