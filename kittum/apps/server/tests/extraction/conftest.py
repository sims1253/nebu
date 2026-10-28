from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.core import FixedClock, Id, new_id
from kittum.documents import Document, Layer, PyMuPDFParser, derive, ingest
from kittum.extraction import JOB_KIND, run_handler
from kittum.ledger import PostgresLedger
from kittum.people import record_roles
from kittum.runs import Commands, PostgresJobQueue, PostgresReceipts, Worker
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.documents.conftest import CachedParser, new_project, savepoint_db
from tests.extraction.support import DOCUMENTS, RECIPE_ID, recorded

if TYPE_CHECKING:
    from kittum.db import Database, Tx
    from kittum.models import LlmBackend

_PARSER = CachedParser(PyMuPDFParser())


def actor(tx: Tx, name: str) -> Id:
    row = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, 'human') returning id",
        (f"{name}-{new_id()}@example.org",),
    ).fetchone()
    assert row is not None
    return row["id"]


@dataclass
class Stored:
    pdf: bytes
    document: Document
    layer: Layer


@dataclass
class Env:
    """A project with a document per fixture PDF, the API, and a worker, on the test's database."""

    tx: Tx
    db: Database
    artifacts: MemoryStore
    clock: FixedClock
    ledger: PostgresLedger
    queue: PostgresJobQueue
    commands: Commands
    client: TestClient
    project_id: Id
    requester: Id
    reviewer: Id
    stored: dict[str, Stored]
    backends: dict[str, LlmBackend]

    def worker(self, backend: LlmBackend | None = None) -> Worker:
        handler = run_handler(
            db=self.db,
            artifacts=self.artifacts,
            commands=self.commands,
            backends={RECIPE_ID: backend} if backend is not None else self.backends,
            clock=self.clock,
        )
        # A long lease, so the heartbeat thread never shares the test's connection.
        return Worker(
            self.db, self.queue, {JOB_KIND: handler}, name="w1", lease=timedelta(minutes=30)
        )

    def events(self, type: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.ledger.events(self.tx, self.project_id) if e.type == type]


@pytest.fixture
def env(pg: Tx) -> Env:
    clock = FixedClock(datetime(2026, 10, 29, 9, tzinfo=UTC))
    artifacts = MemoryStore()
    db = savepoint_db(pg)
    ledger = PostgresLedger(clock)
    queue = PostgresJobQueue(clock)
    receipts = PostgresReceipts()
    stored = {}
    for name, path in DOCUMENTS.items():
        pdf = Path(path).read_bytes()
        document = ingest(pg, artifacts, pdf)
        stored[name] = Stored(pdf, document, derive(pg, document, _PARSER, artifacts=artifacts))
    context = AppContext(
        settings=Settings(database_url="unused", artifact_dir=Path("unused")),
        db=db,
        artifacts=artifacts,
        clock=clock,
        ledger=ledger,
        queue=queue,
        receipts=receipts,
    )
    project_id = new_project(pg)
    requester, reviewer = actor(pg, "methods-lead"), actor(pg, "reviewer")
    # The requester leads the project and the reviewer reviews in it; neither screens, so both
    # may see every run. The client asks as the reviewer.
    record_roles(pg, project_id, requester, "Lead", ("lead",), event_id=new_id())
    record_roles(pg, project_id, reviewer, "Reviewer", ("reviewer",), event_id=new_id())
    client = TestClient(create_app(context), headers={"X-Kittum-Reviewer": str(reviewer)})
    return Env(
        tx=pg,
        db=db,
        artifacts=artifacts,
        clock=clock,
        ledger=ledger,
        queue=queue,
        commands=Commands(ledger, queue, receipts, clock),
        client=client,
        project_id=project_id,
        requester=requester,
        reviewer=reviewer,
        stored=stored,
        backends={RECIPE_ID: recorded("vitamin_c")},
    )
