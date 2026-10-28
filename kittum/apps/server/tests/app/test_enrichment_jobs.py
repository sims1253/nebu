"""The enrichment and open-access jobs when workers race, sources fail for one record, or the
stack runs offline. On responses recorded for the stack; nothing touches the network.

A worker asks the sources with no transaction open and writes only while it still holds the job's
lease, so one that lost its lease while it waited on the network writes nothing over the worker
that took over. Two requests at once queue one run. A source's failure counts against the records
it failed for, not every record of the batch. Recordings replace the real sources only in offline
mode, and never reach the deployment's response cache.
"""

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import dict_row

from kittum.api import AppContext
from kittum.app import (
    ConfigurationError,
    build_gateway,
    enrichment_run,
    job_handlers,
    request_enrichment,
    request_open_access,
)
from kittum.app.demo import DATA
from kittum.core import FixedClock, SystemClock
from kittum.corpus import latest_revision, reports
from kittum.gateway import HttpRequest, RecordedTransport, SourceGateway, request_hash
from kittum.ledger import PostgresLedger
from kittum.runs import JobContext, LeaseLost, PostgresJobQueue, PostgresReceipts, Worker
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.app.record_stack import CITATION, GUIDELINE_REFERENCES, STACK, build_corpus
from tests.documents.conftest import savepoint_db
from tests.enrich.support import CONTACT_EMAIL

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.db import Database, Tx
    from kittum.gateway import HttpResponse
    from kittum.runs import Job

pytestmark = pytest.mark.postgres

LESOTHO_PDF = "https://link.springer.com/content/pdf/10.1186/s12916-026-04943-4.pdf"
DEMO = (
    (DATA / "pubmed_efetch.xml", "pubmed-xml", "pubmed"),
    (DATA / "central_export.ris", "central", "central"),
    CITATION,
)
GUIDELINE = (CITATION, (GUIDELINE_REFERENCES, "ris", "guideline_references"))


class Sources:
    """The recorded sources, with a hook that runs once, before the first request it matches,
    and URLs that fail as an unreachable host would."""

    def __init__(self) -> None:
        self.recorded = RecordedTransport(STACK)
        self.hook: tuple[str, Callable[[], None]] | None = None
        self.down: set[str] = set()

    def send(self, request: HttpRequest) -> HttpResponse:
        if self.hook and self.hook[0] in request.url:
            _, run = self.hook
            self.hook = None
            run()
        if request.url in self.down:
            raise ConnectionError(f"{request.url} is unreachable")
        return self.recorded.send(request)


def _context(db: Database, sources: Sources) -> AppContext:
    artifacts = MemoryStore()
    clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    return AppContext(
        settings=Settings(
            database_url="unused", artifact_dir=Path("unused"), contact_email=CONTACT_EMAIL
        ),
        db=db,
        artifacts=artifacts,
        # Rate-limit waits pass on the gateway's own clock, not on the wall.
        gateway=SourceGateway(
            database=db,
            transport=sources,
            artifacts=artifacts,
            buckets="memory",
            cache="memory",
            clock=clock,
            sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
        ),
        ledger=PostgresLedger(),
        queue=PostgresJobQueue(),
        receipts=PostgresReceipts(),
    )


def _only_this_project(tx: Tx, project_id: Any) -> None:
    """Other projects' jobs in the shared database stay out of this test's claims (rolled back)."""
    tx.execute(
        "update job set status = 'failed' where project_id <> %s"
        " and status in ('queued', 'running')",
        (project_id,),
    )


def _take_over(tx: Tx, job: Job, ctx: AppContext) -> None:
    """Another worker takes the job over after this one's lease ran out, and finishes it."""
    tx.execute(
        "update job set lease_until = %s where id = %s",
        (datetime(2000, 1, 1, tzinfo=UTC), job.job_id),
    )
    other = Worker(ctx.db, ctx.queue, job_handlers(ctx), name="B", lease=timedelta(hours=1))  # type: ignore[arg-type]
    assert other.run_once() is True


def _claim(tx: Tx, ctx: AppContext, kind: str) -> Job:
    assert ctx.queue is not None
    job = ctx.queue.claim(tx, (kind,), "A", timedelta(hours=1))
    assert job is not None
    return job


def test_an_enrichment_worker_that_lost_its_lease_writes_nothing(pg: Tx) -> None:
    sources = Sources()
    ctx = _context(savepoint_db(pg), sources)
    ledger, clock = PostgresLedger(), SystemClock()
    project, lead = build_corpus(
        pg, "lease", GUIDELINE, ledger=ledger, clock=clock, artifacts=ctx.artifacts
    )
    _only_this_project(pg, project)
    assert ctx.queue is not None
    run = request_enrichment(
        pg,
        project,
        actor_id=lead,
        trigger="corpus_built",
        ledger=ledger,
        queue=ctx.queue,
        clock=clock,
    )
    job = _claim(pg, ctx, "enrich.revision")
    # While worker A waits on OpenAlex, its lease runs out and worker B does the whole run.
    sources.hook = ("api.openalex.org", lambda: _take_over(pg, job, ctx))
    with pytest.raises(LeaseLost):
        job_handlers(ctx)["enrich.revision"](job, JobContext(job=job))

    view = enrichment_run(pg, project, run.id)
    assert (view.status, view.done, view.retracted) == ("done", 4, 1)
    events = [e.type for e in ledger.events(pg, project) if e.type.startswith("enrichment.")]
    assert events == [
        "enrichment.requested",
        "enrichment.records_enriched",
        "enrichment.finished",
    ]


def test_a_retrieval_worker_that_lost_its_lease_keeps_off_the_acquired_report(
    pg: Tx, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = Sources()
    ctx = _context(savepoint_db(pg), sources)
    ledger, clock = PostgresLedger(), SystemClock()
    project, lead = build_corpus(
        pg, "lease", GUIDELINE, ledger=ledger, clock=clock, artifacts=ctx.artifacts
    )
    _only_this_project(pg, project)
    assert ctx.queue is not None
    request_enrichment(
        pg,
        project,
        actor_id=lead,
        trigger="corpus_built",
        ledger=ledger,
        queue=ctx.queue,
        clock=clock,
    )
    worker = Worker(ctx.db, ctx.queue, job_handlers(ctx), name="W", lease=timedelta(hours=1))
    assert worker.run_once() is True  # the enrichment
    revision = latest_revision(pg, project)
    assert revision is not None
    (lesotho,) = [
        r.report_id
        for r in reports(pg, project, revision.id)
        if r.records[0].doi == "10.1186/s12916-026-04943-4"
    ]
    # The Lesotho trial is sought for retrieval (screening is not the point here).
    monkeypatch.setattr("kittum.app.open_access.sought_for_retrieval", lambda *_: [lesotho])
    run = request_open_access(
        pg, project, ctx.screening(), actor_id=lead, ledger=ledger, queue=ctx.queue, clock=clock
    )
    job = _claim(pg, ctx, "retrieval.open_access")
    # While worker A waits on Europe PMC, worker B takes over, fetches, and attaches the PDF.
    sources.hook = ("europepmc.org/articles", lambda: _take_over(pg, job, ctx))
    with pytest.raises(LeaseLost):
        job_handlers(ctx)["retrieval.open_access"](job, JobContext(job=job))

    row = pg.execute(
        "select outcome, acquisition_id, attempts from retrieval_run_report where run_id = %s",
        (run.id,),
    ).fetchone()
    assert row is not None
    assert row["outcome"] == "acquired" and row["acquisition_id"] is not None
    assert [a["outcome"] for a in row["attempts"]] == ["http_error", "acquired"]
    types = [e.type for e in ledger.events(pg, project)]
    assert types.count("document.acquired") == 1
    assert types.count("document.acquisition_attempted") == 1
    assert types.count("project.full_text_attached") == 1


def test_a_source_failure_counts_only_against_the_records_it_failed_for(pg: Tx) -> None:
    sources = Sources()
    ctx = _context(savepoint_db(pg), sources)
    ledger, clock = PostgresLedger(), SystemClock()
    project, lead = build_corpus(
        pg, "failures", DEMO, ledger=ledger, clock=clock, artifacts=ctx.artifacts
    )
    _only_this_project(pg, project)
    # Crossref is unreachable for one DOI, which the PubMed search and the CENTRAL export share.
    sources.down.add(
        "https://api.crossref.org/works/10.1001%2Fjamanetworkopen.2024.18120"
        "?mailto=test%40kittum.dev"
    )
    assert ctx.queue is not None
    run = request_enrichment(
        pg,
        project,
        actor_id=lead,
        trigger="corpus_built",
        ledger=ledger,
        queue=ctx.queue,
        clock=clock,
    )
    worker = Worker(ctx.db, ctx.queue, job_handlers(ctx), name="W", lease=timedelta(hours=1))
    assert worker.run_once() is True

    rows = pg.execute(
        "select e.doi, r.asked, r.answered, r.failed from enrichment_run_record r"
        " join record_enrichment e on e.occurrence_id = r.occurrence_id where r.run_id = %s",
        (run.id,),
    ).fetchall()
    failed = {r["doi"] for r in rows if any(f["source"] == "crossref" for f in r["failed"])}
    assert failed == {"10.1001/jamanetworkopen.2024.18120"}
    # Crossref answered the others; many of them gave no field, and none of them failed.
    quiet = [r for r in rows if "crossref" in r["asked"] and "crossref" not in r["answered"]]
    assert len(quiet) > 5
    crossref = {s.source: s for s in enrichment_run(pg, project, run.id).sources}["crossref"]
    asked_jama = sum(1 for r in rows if r["doi"] == "10.1001/jamanetworkopen.2024.18120")
    assert crossref.failed == asked_jama == 2
    assert crossref.last_error and "unreachable" in crossref.last_error


def test_two_requests_at_once_queue_one_enrichment(database: Database) -> None:
    ledger, clock, queue = PostgresLedger(), SystemClock(), PostgresJobQueue()
    with database.transaction() as tx:
        project, lead = build_corpus(
            tx, "race", (CITATION,), ledger=ledger, clock=clock, artifacts=MemoryStore()
        )
    url = os.environ["KITTUM_DATABASE_URL"]

    def request(conn: psycopg.Connection[dict[str, Any]]) -> Any:
        return request_enrichment(
            conn,
            project,
            actor_id=lead,
            trigger="requested",
            ledger=ledger,
            queue=queue,
            clock=clock,
        ).id

    first = psycopg.Connection[dict[str, Any]].connect(url, row_factory=dict_row)
    second = psycopg.Connection[dict[str, Any]].connect(url, row_factory=dict_row)
    watcher = psycopg.Connection[dict[str, Any]].connect(url, row_factory=dict_row, autocommit=True)
    try:
        # The first request is made and not yet committed when the second comes in.
        one = request(first)
        found: list[Any] = []
        other = threading.Thread(target=lambda: (found.append(request(second)), second.commit()))
        other.start()
        pid = second.info.backend_pid
        deadline = time.monotonic() + 10
        while other.is_alive() and time.monotonic() < deadline:
            waiting = watcher.execute(
                "select 1 from pg_locks where pid = %s and not granted", (pid,)
            ).fetchone()
            if waiting:
                break
            time.sleep(0.02)
        first.commit()
        other.join(timeout=10)
        assert found == [one]
        runs = watcher.execute(
            "select count(*) as n from enrichment_run where project_id = %s", (project,)
        ).fetchone()
        assert runs is not None and runs["n"] == 1
    finally:
        watcher.execute(
            "update job set status = 'failed' where project_id = %s and status = 'queued'",
            (project,),
        )
        for conn in (first, second, watcher):
            conn.close()


def test_recordings_replace_the_sources_only_offline_and_stay_out_of_the_cache(pg: Tx) -> None:
    db = savepoint_db(pg)
    artifacts = MemoryStore()

    def settings(**kwargs: Any) -> Settings:
        return Settings(database_url="unused", artifact_dir=Path("unused"), **kwargs)

    with pytest.raises(ConfigurationError, match="KITTUM_OFFLINE"):
        build_gateway(settings(gateway_recordings=STACK), db, artifacts, SystemClock())
    with pytest.raises(ConfigurationError, match="KITTUM_OFFLINE"):
        build_gateway(settings(offline=True), db, artifacts, SystemClock())

    gateway = build_gateway(
        settings(gateway_recordings=STACK, offline=True), db, artifacts, SystemClock()
    )
    request = HttpRequest(
        url="https://api.unpaywall.org/v2/10.1186%2Fs12916-026-04943-4?email=test%40kittum.dev",
        headers=(("Accept", "application/json"),),
    )
    first = gateway.fetch("unpaywall", request)
    again = gateway.fetch("unpaywall", request)
    assert first.status == 200 and not first.from_cache and again.from_cache
    cached = pg.execute(
        "select 1 from gateway_cache where request_hash = %s", (request_hash(request),)
    ).fetchone()
    assert cached is None
