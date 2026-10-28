"""Job queue behaviour, shared by the Postgres queue and the in-memory fake."""

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from kittum.runs import LeaseLost, StageKeyConflict

if TYPE_CHECKING:
    from tests.runs.conftest import QueueHarness

LEASE = timedelta(seconds=30)
OUTPUT = "ab" * 32


def test_a_stage_is_published_once(jobs: QueueHarness) -> None:
    first = jobs.queue.enqueue(jobs.tx, jobs.spec("judge:record-17:v1", record=17))
    again = jobs.queue.enqueue(jobs.tx, jobs.spec("judge:record-17:v1", record=17))
    assert again.job_id == first.job_id

    with pytest.raises(StageKeyConflict):
        jobs.queue.enqueue(jobs.tx, jobs.spec("judge:record-17:v1", record=18))

    claimed = jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE)
    assert claimed is not None and claimed.job_id == first.job_id
    assert jobs.queue.claim(jobs.tx, (jobs.kind,), "w2", LEASE) is None
    done = jobs.queue.complete(jobs.tx, claimed, OUTPUT)
    # Publishing the finished stage again does not queue it again.
    assert jobs.queue.enqueue(jobs.tx, jobs.spec("judge:record-17:v1", record=17)) == done
    assert jobs.queue.claim(jobs.tx, (jobs.kind,), "w2", LEASE) is None


def test_failures_back_off_exponentially_then_dead_letter(jobs: QueueHarness) -> None:
    job = jobs.queue.enqueue(jobs.tx, jobs.spec("parse:doc-1"))
    waits = []
    for attempt in (1, 2):
        claimed = jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE)
        assert claimed is not None and claimed.attempt == attempt
        failed = jobs.queue.fail(jobs.tx, claimed, f"HTTP 503 on attempt {attempt}")
        assert (failed.status, failed.last_error) == ("queued", f"HTTP 503 on attempt {attempt}")
        wait = failed.run_after - jobs.clock.now()
        waits.append(wait)
        # Not due before its backoff ends.
        jobs.clock.advance(wait - timedelta(microseconds=1))
        assert jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE) is None
        jobs.clock.advance(timedelta(microseconds=1))

    # Base 10 s with jitter between half and all of 10 s, then of 20 s.
    assert timedelta(seconds=5) <= waits[0] <= timedelta(seconds=10)
    assert timedelta(seconds=10) <= waits[1] <= timedelta(seconds=20)

    last = jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE)
    assert last is not None and last.attempt == 3
    dead = jobs.queue.fail(jobs.tx, last, "HTTP 503 on attempt 3")
    assert dead.status == "dead"
    jobs.clock.advance(timedelta(days=1))
    assert jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE) is None
    assert jobs.queue.get(jobs.tx, job.job_id).status == "dead"


def test_a_permanent_failure_is_not_retried(jobs: QueueHarness) -> None:
    jobs.queue.enqueue(jobs.tx, jobs.spec("parse:encrypted.pdf"))
    claimed = jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE)
    assert claimed is not None
    failed = jobs.queue.fail(jobs.tx, claimed, "PDF is encrypted", retryable=False)
    assert failed.status == "failed"
    jobs.clock.advance(timedelta(days=1))
    assert jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE) is None


def test_an_expired_lease_moves_the_job_and_fences_the_old_owner(jobs: QueueHarness) -> None:
    jobs.queue.enqueue(jobs.tx, jobs.spec("judge:record-3"))
    a = jobs.queue.claim(jobs.tx, (jobs.kind,), "worker-a", LEASE)
    assert a is not None

    # Heartbeats keep the job with A past its first lease.
    jobs.clock.advance(timedelta(seconds=20))
    jobs.queue.heartbeat(jobs.tx, a, LEASE)
    jobs.clock.advance(timedelta(seconds=20))
    assert jobs.queue.claim(jobs.tx, (jobs.kind,), "worker-b", LEASE) is None

    # A stops heartbeating; once its lease runs out, B takes over.
    jobs.clock.advance(timedelta(seconds=10))
    b = jobs.queue.claim(jobs.tx, (jobs.kind,), "worker-b", LEASE)
    assert b is not None
    assert (b.job_id, b.attempt, b.lease_owner) == (a.job_id, 2, "worker-b")

    for late in (
        lambda: jobs.queue.heartbeat(jobs.tx, a, LEASE),
        lambda: jobs.queue.complete(jobs.tx, a, OUTPUT),
        lambda: jobs.queue.fail(jobs.tx, a, "late"),
    ):
        with pytest.raises(LeaseLost):
            late()
    done = jobs.queue.complete(jobs.tx, b, OUTPUT)
    assert (done.status, done.output_hash, done.lease_owner) == ("succeeded", OUTPUT, "worker-b")


def test_a_job_whose_last_lease_expires_is_dead_lettered(jobs: QueueHarness) -> None:
    job = jobs.queue.enqueue(jobs.tx, jobs.spec("judge:record-4"))
    for _ in range(3):
        assert jobs.queue.claim(jobs.tx, (jobs.kind,), "crashy", LEASE) is not None
        jobs.clock.advance(LEASE)

    assert jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE) is None
    dead = jobs.queue.get(jobs.tx, job.job_id)
    assert (dead.status, dead.attempt) == ("dead", 3)
    assert dead.last_error and "expired on the last attempt" in dead.last_error


def test_claims_only_due_jobs_of_the_asked_kinds_in_order(jobs: QueueHarness) -> None:
    first = jobs.queue.enqueue(jobs.tx, jobs.spec("s1"))
    jobs.clock.advance(timedelta(seconds=1))
    second = jobs.queue.enqueue(jobs.tx, jobs.spec("s2"))
    assert jobs.queue.claim(jobs.tx, ("other.kind",), "w1", LEASE) is None
    got = [jobs.queue.claim(jobs.tx, (jobs.kind,), "w1", LEASE) for _ in range(3)]
    assert [j.job_id if j else None for j in got] == [first.job_id, second.job_id, None]
