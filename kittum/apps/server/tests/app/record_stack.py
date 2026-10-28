"""Record the live responses that the stack replays from `fixtures/stack/`.

Run once, by hand, with a database available (the projects are rolled back afterwards):

    KITTUM_DATABASE_URL=... uv run python -m tests.app.record_stack

This is not a test. For each project the tests and the end-to-end journeys build, it imports the
same searches in the same order, builds the corpus, runs the enrichment job, and walks the
open-access waterfall for every report, all through the real HTTP transport. Every response is
kept under the URL that asked for it, with its status and content type; a PDF that the repository
already holds is referenced, not copied. The contact address is `support.CONTACT_EMAIL`, and no
API key is used, so none can end up in a recording.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import psycopg
from psycopg.rows import dict_row

from kittum.app import AppContext, create_project, ingest_corpus, publish_protocol
from kittum.app.demo import DATA, PDFS, PROTOCOL
from kittum.app.enrichment import ENRICHMENT_JOB, enrichment_handler, request_enrichment
from kittum.app.open_access import locations
from kittum.core import SystemClock, sha256_hex
from kittum.corpus import latest_revision, reports
from kittum.documents import acquire
from kittum.gateway import HttpRequest, HttpResponse, HttpxTransport
from kittum.gateway.testing import PassThroughGateway
from kittum.ledger import PostgresLedger
from kittum.runs import JobContext, PostgresJobQueue
from kittum.search import SearchPlan, import_file
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.documents.conftest import SavepointDatabase
from tests.enrich.support import CONTACT_EMAIL, FIXTURES

if TYPE_CHECKING:
    from kittum.core import Clock, Id
    from kittum.db import Database, Tx
    from kittum.ledger import Ledger
    from kittum.storage import ArtifactStore

STACK = FIXTURES / "stack"
GUIDELINE_REFERENCES = FIXTURES / "guideline_references.ris"

# (file, format, source) per search, in import order: the order fixes the batched URLs.
type Searches = tuple[tuple[Path, str, str], ...]
CITATION = (DATA / "citation_search.ris", "ris", "citation_search")
# Each project, and whether its open-access waterfall is walked too. Only the enrichment journey
# fetches full texts, and the PDFs it gets are the CC BY ones the repository holds already.
SCENARIOS: dict[str, tuple[Searches, bool]] = {
    # extraction.spec.ts: the citation search alone.
    "citation": ((CITATION,), False),
    # The enrichment test and enrichment.spec.ts: the citation search, then a guideline's
    # reference list with a trial the Lancet retracted.
    "citation_and_guideline": (
        (CITATION, (GUIDELINE_REFERENCES, "ris", "guideline_references")),
        True,
    ),
    # setup.spec.ts: the demo's three searches.
    "demo": (
        (
            (DATA / "pubmed_efetch.xml", "pubmed-xml", "pubmed"),
            (DATA / "central_export.ris", "central", "central"),
            CITATION,
        ),
        False,
    ),
}

_EXTENSIONS = {"application/json": ".json", "application/pdf": ".pdf", "text/html": ".html"}


class RecordingTransport:
    """Sends real requests and keeps each response under the URL that produced it."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._inner = HttpxTransport(contact_email=CONTACT_EMAIL)
        self._known = {sha256_hex(p.read_bytes()): p for p in PDFS.glob("*.pdf")}
        self.manifest: dict[str, dict[str, Any]] = {}

    def send(self, request: HttpRequest) -> HttpResponse:
        response = self._inner.send(request)
        content_type = next(
            (v.split(";")[0].strip() for k, v in response.headers if k.lower() == "content-type"),
            None,
        )
        known = self._known.get(response.body_hash)
        if known is not None:
            name = str(Path("..", "..", "..", "documents", "fixtures", known.name))
        else:
            name = sha256_hex(request.url.encode())[:16] + _EXTENSIONS.get(content_type or "", "")
            (self._directory / name).write_bytes(response.body)
        self.manifest[request.url] = {
            "file": name,
            "status": response.status,
            "content_type": content_type,
        }
        return response

    def write_manifest(self) -> None:
        path = self._directory / "manifest.json"
        existing = json.loads(path.read_text()) if path.exists() else {}
        existing.update(self.manifest)
        path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")


def build_corpus(
    tx: Tx,
    name: str,
    searches: Searches,
    *,
    ledger: Ledger,
    clock: Clock,
    artifacts: ArtifactStore,
) -> tuple[Id, Id]:
    """A project with the example protocol, `searches` imported in order, and its corpus.
    Returns the project and its lead."""
    project_id, lead = create_project(tx, name=name, lead_name="Rec", ledger=ledger)
    publish_protocol(tx, project_id, PROTOCOL.read_text(), approved_by=lead, ledger=ledger)
    for path, format, source in searches:
        plan = SearchPlan(topic_id="TRIALS", source=source, platform=source, strategy="recorded")
        import_file(
            tx,
            project_id,
            plan,
            path.read_bytes(),
            format,
            ledger=ledger,
            actor_id=lead,
            artifacts=artifacts,
            clock=clock,
        )
    ingest_corpus(tx, project_id, actor_id=lead, ledger=ledger)
    return project_id, lead


def _scenario(
    tx: Tx, name: str, searches: Searches, waterfall: bool, gateway: PassThroughGateway
) -> None:
    ledger = PostgresLedger()
    clock = SystemClock()
    artifacts = MemoryStore()
    project_id, lead = build_corpus(
        tx, f"record {name}", searches, ledger=ledger, clock=clock, artifacts=artifacts
    )
    queue = PostgresJobQueue()
    run = request_enrichment(
        tx,
        project_id,
        actor_id=lead,
        trigger="corpus_built",
        ledger=ledger,
        queue=queue,
        clock=clock,
    )
    ctx = AppContext(
        settings=Settings(
            database_url="unused", artifact_dir=Path("unused"), contact_email=CONTACT_EMAIL
        ),
        db=cast("Database", SavepointDatabase(tx)),
        artifacts=artifacts,
        clock=clock,
        gateway=gateway,
        ledger=ledger,
        queue=queue,
    )
    row = tx.execute("select job_id from enrichment_run where id = %s", (run.id,)).fetchone()
    assert row is not None
    tx.execute(
        "update job set run_after = now() + interval '1 day' where id <> %s", (row["job_id"],)
    )
    claimed = queue.claim(tx, (ENRICHMENT_JOB,), "recorder", timedelta(hours=1))
    assert claimed is not None and claimed.job_id == row["job_id"]
    output = enrichment_handler(ctx, gateway, ledger, queue)(claimed, JobContext(job=claimed))
    assert output.publish is not None
    output.publish(tx)
    revision = latest_revision(tx, project_id)
    assert revision is not None
    if not waterfall:
        return
    for report in reports(tx, project_id, revision.id):
        found = locations(tx, project_id, report.report_id, on=clock.now().date())
        if found:
            acquire(
                tx,
                gateway=gateway,
                artifacts=artifacts,
                ledger=ledger,
                clock=clock,
                project_id=project_id,
                actor_id=lead,
                locations=found,
            )


def main() -> None:
    import os

    STACK.mkdir(exist_ok=True)
    transport = RecordingTransport(STACK)
    gateway = PassThroughGateway(transport)
    url = os.environ["KITTUM_DATABASE_URL"]
    with psycopg.Connection[dict[str, Any]].connect(url, row_factory=dict_row) as connection:
        try:
            for name, (searches, waterfall) in SCENARIOS.items():
                _scenario(connection, name, searches, waterfall, gateway)
        finally:
            connection.rollback()
    transport.write_manifest()
    print(f"recorded {len(transport.manifest)} responses in {STACK}")


if __name__ == "__main__":
    main()
