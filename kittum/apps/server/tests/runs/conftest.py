"""Fixtures for the runs tests: one queue harness per store, and a committing database."""

import os
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from kittum.core import FixedClock, new_id
from kittum.db import Database
from kittum.db.testing import null_tx
from kittum.ledger.testing import seed_project
from kittum.runs import JobSpec, PostgresJobQueue, RetryPolicy
from kittum.runs.testing import InMemoryJobQueue

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kittum.core import Id
    from kittum.db import Tx
    from kittum.runs import JobQueue

START = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
RETRY = RetryPolicy(max_attempts=3, base=timedelta(seconds=10), cap=timedelta(minutes=1))


@dataclass
class QueueHarness:
    queue: JobQueue
    tx: Tx
    clock: FixedClock
    project_id: Id
    kind: str

    def spec(self, stage_key: str, **payload: Any) -> JobSpec:
        return JobSpec(
            project_id=self.project_id, kind=self.kind, stage_key=stage_key, payload=payload
        )


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def jobs(request: pytest.FixtureRequest) -> QueueHarness:
    clock = FixedClock(START)
    # A fresh kind per test, so jobs left in a shared database never get claimed here.
    kind = f"test.{new_id()}"
    if request.param == "memory":
        queue = InMemoryJobQueue(clock, RETRY, random.Random(7))
        return QueueHarness(queue, null_tx(), clock, new_id(), kind)
    pg: Tx = request.getfixturevalue("pg")
    project_id, _ = seed_project(pg)
    return QueueHarness(
        PostgresJobQueue(clock, RETRY, random.Random(7)), pg, clock, project_id, kind
    )


@pytest.fixture
def database() -> Iterator[Database]:
    """A pool on KITTUM_DATABASE_URL whose transactions commit. Use fresh IDs and kinds."""
    url = os.environ.get("KITTUM_DATABASE_URL")
    if not url:
        pytest.skip("KITTUM_DATABASE_URL is not set")
    db = Database(url, max_size=6)
    try:
        yield db
    finally:
        db.close()
