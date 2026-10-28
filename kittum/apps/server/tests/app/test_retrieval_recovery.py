"""Getting full texts that open access did not give, through the worker and the API.

On recorded real responses (`tests/enrich/fixtures/stack/`): a citation search with two CC BY
trials and a registry entry, and a guideline's reference list with a trial the Lancet retracted.

Retry: Europe PMC answers enrichment with the 503 and the message it sent during the outage of
28 October 2026, and PubMed Central's bucket is unreachable during retrieval. The lead sees which
sources failed, in their own words, and once they are back retries them: the records Europe PMC
failed for are enriched again, then the report still without a full text is retrieved again, all
in the ledger, with no new run of the whole corpus.

Missing full texts: the lead uploads a folder of PDFs at once. One matches by the DOI in its file
name, one by its first pages, a supplement named after a trial is held because its pages show
another document, and a scan without text is held until the lead assigns it. The report left
without a full text goes out as CSV and RIS, with every source tried and why each failed.

Reviewers get 403 from every route of it.
"""

import csv
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pymupdf
import pytest
import rispy
import yaml
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app import job_handlers
from kittum.app.demo import DATA, PDFS, PROTOCOL
from kittum.core import FixedClock
from kittum.corpus import latest_revision, reports
from kittum.gateway import RecordedTransport, SourceGateway
from kittum.gateway.testing import response
from kittum.ledger import PostgresLedger
from kittum.runs import PostgresJobQueue, PostgresReceipts, Worker
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.app.record_stack import GUIDELINE_REFERENCES, STACK
from tests.documents.conftest import FIXTURES as PDF_FIXTURES
from tests.documents.conftest import savepoint_db
from tests.enrich.support import CONTACT_EMAIL, FIXTURES

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.db import Tx
    from kittum.gateway import HttpRequest, HttpResponse

pytestmark = pytest.mark.postgres

LESOTHO_DOI = "10.1186/s12916-026-04943-4"
VITAMIN_C_DOI = "10.1186/s12916-026-05037-x"
LANCET_DOI = "10.1016/s0140-6736(20)31180-6"
OUTAGE = (FIXTURES / "europepmc_outage_503.json").read_bytes()
CHECKLIST = (PDF_FIXTURES / "jmir" / "app2.pdf").read_bytes()


class Sources:
    """The recorded sources, with Europe PMC's search and PubMed Central's bucket down at will."""

    def __init__(self) -> None:
        self.recorded = RecordedTransport(STACK)
        self.europepmc_down = False
        self.pmc_down = False

    def send(self, request: HttpRequest) -> HttpResponse:
        if self.europepmc_down and "europepmc/webservices/rest/search" in request.url:
            return response(503, OUTAGE)
        if self.pmc_down and "pmc-oa-opendata" in request.url:
            raise ConnectionError("pmc-oa-opendata.s3.amazonaws.com is unreachable")
        return self.recorded.send(request)


class Review:
    """A project on the stack's searches, its lead and a reviewer, a worker, and the API."""

    def __init__(self, pg: Tx, sources: Sources, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
        db = savepoint_db(pg)
        artifacts = MemoryStore()
        self.pg, self.db, self.ledger = pg, db, PostgresLedger()
        ctx = AppContext(
            settings=Settings(
                database_url="unused", artifact_dir=Path("unused"), contact_email=CONTACT_EMAIL
            ),
            db=db,
            artifacts=artifacts,
            gateway=SourceGateway(
                database=db,
                transport=sources,
                artifacts=artifacts,
                buckets="memory",
                cache="memory",
                max_attempts=2,
                clock=clock,
                sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
            ),
            ledger=self.ledger,
            queue=PostgresJobQueue(),
            receipts=PostgresReceipts(),
        )
        pg.execute(
            "update job set run_after = now() + interval '1 day'"
            " where status in ('queued', 'running')"
        )
        self.worker = Worker(db, ctx.queue, job_handlers(ctx), name="t", lease=timedelta(hours=1))  # type: ignore[arg-type]
        self.client = TestClient(create_app(ctx))
        project = self.ok(
            self.client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan Lee"}), 201
        )
        self.pid = project["id"]
        self.lead = project["members"][0]["actor_id"]
        body = {"display_name": "Sam", "roles": ["reviewer"], "added_by": self.lead}
        self.sam = self.ok(self.client.post(f"/api/projects/{self.pid}/members", json=body), 201)[
            "actor_id"
        ]
        version = self.ok(
            self.client.post(
                f"/api/projects/{self.pid}/protocol-versions",
                json=yaml.safe_load(PROTOCOL.read_text()),
            ),
            201,
        )
        self.ok(
            self.client.post(
                f"/api/projects/{self.pid}/protocol-versions/{version['id']}/publish",
                json={"approved_by": self.lead},
            )
        )
        for source, path in (
            ("citation_search", DATA / "citation_search.ris"),
            ("guideline_references", GUIDELINE_REFERENCES),
        ):
            plan = {"topic_id": "TRIALS", "source": source, "platform": source, "strategy": "x"}
            body = {"plan": plan, "data": path.read_text(), "format": "ris", "actor_id": self.lead}
            self.ok(self.client.post(f"/api/projects/{self.pid}/search/import", json=body))
        self.ok(
            self.client.post(
                f"/api/projects/{self.pid}/corpus/ingest", json={"actor_id": self.lead}
            ),
            201,
        )
        revision = latest_revision(pg, self.pid)
        assert revision is not None
        found = reports(pg, self.pid, revision.id)
        self.reports = {
            next((r.doi.lower() for r in report.records if r.doi), "registry"): str(
                report.report_id
            )
            for report in found
        }
        # Every report is sought for retrieval; screening is not the point here.
        sought = [report.report_id for report in found]
        monkeypatch.setattr("kittum.app.open_access.sought_for_retrieval", lambda *_: sought)

    def ok(self, answer: Any, status: int = 200) -> Any:
        assert answer.status_code == status, answer.text
        return answer.json() if answer.content else None

    def get(self, path: str, as_: str | None = None) -> Any:
        return self.ok(
            self.client.get(
                f"/api/projects/{self.pid}/{path}", headers={"X-Kittum-Reviewer": as_ or self.lead}
            )
        )

    def post(self, path: str, status: int = 202, **body: Any) -> Any:
        return self.ok(
            self.client.post(
                f"/api/projects/{self.pid}/{path}", json={"actor_id": self.lead} | body
            ),
            status,
        )

    def work(self) -> int:
        ran = 0
        while self.worker.run_once():
            ran += 1
        return ran

    def events(self, kind: Callable[[str], bool]) -> list[Any]:
        with self.db.transaction() as tx:
            return [e for e in self.ledger.events(tx, self.pid) if kind(e.type)]


def test_a_lead_retries_the_sources_that_were_down_without_a_new_run(
    pg: Tx, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = Sources()
    sources.europepmc_down = True
    review = Review(pg, sources, monkeypatch)
    assert review.work() == 1  # the enrichment, with Europe PMC down

    run = review.get("enrichment/runs/latest")
    europepmc = {s["source"]: s for s in run["sources"]}["europepmc"]
    assert (europepmc["asked"], europepmc["answered"], europepmc["failed"]) == (3, 0, 3)
    assert europepmc["last_failure"] == {
        "source": "europepmc",
        "reason": "HTTP 503 after 2 attempts",
        "kind": "http_error",
        "status": 503,
        "message": "Search service is temporarily unavailable. Please retry later.",
    }

    # Retrieval runs while PubMed Central's bucket is unreachable too. Without Europe PMC there is
    # no PMCID, so PubMed Central's ID converter finds it first.
    sources.pmc_down = True
    review.post("retrieval/open-access")
    assert review.work() == 1
    first = {r["report_id"]: r for r in review.get("retrieval/open-access/latest")["reports"]}
    lancet = review.reports[LANCET_DOI]
    assert first[lancet]["outcome"] == "not_acquired"
    (pmc,) = [a for a in first[lancet]["attempts"] if a["source"] == "pmc"]
    assert (pmc["kind"], pmc["outcome"], pmc["status"]) == ("lookup", "unavailable", None)
    assert "pmc-oa-opendata" in pmc["url"]
    still_missing = [r for r, v in first.items() if v["outcome"] != "acquired"]

    # What a retry would ask again, by source, in the sources' own words.
    preview = review.get("retrieval/retry")
    assert preview["occurrences"] == 3
    assert {s["source"]: (s["records"], s["reports"]) for s in preview["sources"]} == {
        "europepmc": (3, 0),
        "pmc": (0, sum(1 for r in still_missing if r != review.reports["registry"])),
    }
    assert preview["sources"][0]["message"] == (
        "Search service is temporarily unavailable. Please retry later."
    )

    # Nobody else may retry, nor see what failed.
    assert (
        review.client.get(
            f"/api/projects/{review.pid}/retrieval/retry", headers={"X-Kittum-Reviewer": review.sam}
        ).status_code
        == 403
    )
    refused = review.client.post(
        f"/api/projects/{review.pid}/retrieval/retry", json={"actor_id": review.sam}
    )
    assert refused.status_code == 403

    # The sources are back. One retry: Europe PMC for its three records, then retrieval of the
    # reports still without a full text.
    sources.europepmc_down = sources.pmc_down = False
    plan = review.post("retrieval/retry")
    assert (plan["occurrences"], plan["sources"]) == (3, ["europepmc", "pmc"])
    assert plan["enrichment_run_id"] and plan["retrieval_run_id"] is None
    # While the retry enriches, nobody can start a retrieval that would take its follow-up's place.
    review.post("retrieval/open-access", 409)
    assert review.work() == 2  # the enrichment, then the retrieval it queued

    rerun = review.get("enrichment/runs/latest")
    assert (rerun["id"], rerun["trigger"], rerun["records"], rerun["done"]) == (
        plan["enrichment_run_id"],
        "retry",
        3,
        3,
    )
    assert {s["source"]: s for s in rerun["sources"]}["europepmc"]["failed"] == 0
    lesotho = review.get("enrichment/records")
    assert all("europepmc" in flag["sources"] for flag in lesotho if flag["sources"])
    retried = review.get("retrieval/open-access/latest")
    assert retried["trigger"] == "retry"
    assert [r["report_id"] for r in retried["reports"]] == [
        r for r in still_missing if r != review.reports["registry"]
    ]
    # The reports the retry did not take keep what the first run did for them.
    assert {r["report_id"] for r in retried["earlier"]} == set(first) - {
        r["report_id"] for r in retried["reports"]
    }
    assert all(r == first[r["report_id"]] for r in retried["earlier"])
    (again,) = [r for r in retried["reports"] if r["report_id"] == lancet]
    assert [a["outcome"] for a in again["attempts"] if a["source"] == "pmc"] == ["none"]

    # Nothing failed this time, so there is nothing left to retry.
    assert review.get("retrieval/retry") == {"occurrences": 0, "reports": 0, "sources": []}
    review.post("retrieval/retry", 409)

    requested = review.events(lambda t: t == "retrieval.retry_requested")
    assert [s["source"] for s in requested[0].payload["sources"]] == ["europepmc", "pmc"]
    (enrichment,) = [
        e
        for e in review.events(lambda t: t == "enrichment.requested")
        if e.payload["trigger"] == "retry"
    ]
    assert len(enrichment.payload["occurrence_ids"]) == 3
    triggers = [
        e.payload["trigger"]
        for e in review.events(lambda t: t == "retrieval.open_access_requested")
    ]
    assert triggers == ["requested", "retry"]


def _scan() -> bytes:
    """A one-page PDF without a text layer, as a scanner makes."""
    with pymupdf.open() as document:
        page = document.new_page()
        page.draw_rect(pymupdf.Rect(72, 72, 300, 200), fill=(0.2, 0.2, 0.2))
        return document.tobytes()


def test_a_lead_uploads_a_folder_of_pdfs_and_exports_what_is_still_missing(
    pg: Tx, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = Review(pg, Sources(), monkeypatch)
    assert review.work() == 1  # the enrichment

    def upload(name: str, pdf: bytes, status: int = 201) -> Any:
        return review.ok(
            review.client.post(
                f"/api/projects/{review.pid}/retrieval/uploads",
                params={"actor_id": review.lead, "file_name": name, "licence": "CC BY 4.0"},
                content=pdf,
                headers={"Content-Type": "application/pdf"},
            ),
            status,
        )

    by_name = upload(
        f"{VITAMIN_C_DOI.replace('/', '_')}.pdf",
        (PDFS / "li2026-vitamin-c-kidney-biopsy-rct.pdf").read_bytes(),
    )
    assert (by_name["status"], by_name["matched_by"], by_name["report_id"]) == (
        "attached",
        "file_name",
        review.reports[VITAMIN_C_DOI],
    )
    by_content = upload("scan-0042.pdf", (PDFS / "gerber2026-lesotho-t2d-crct.pdf").read_bytes())
    assert (by_content["status"], by_content["matched_by"], by_content["report_id"]) == (
        "attached",
        "content",
        review.reports[LESOTHO_DOI],
    )
    # The file is named after the Lancet trial, but it is a JMIR trial's CONSORT checklist.
    named_wrongly = upload("10.1016_s0140-6736(20)31180-6.pdf", CHECKLIST)
    assert named_wrongly["status"] == "held"
    assert named_wrongly["reason"].startswith("Its file name names a report, but its first pages")
    assert [c["report_id"] for c in named_wrongly["candidates"]] == [review.reports[LANCET_DOI]]
    scan = upload("registry-entry.pdf", _scan())
    assert (scan["status"], scan["candidates"]) == ("held", [])
    assert upload("readme.pdf", b"not a pdf", 415)
    refused = review.client.post(
        f"/api/projects/{review.pid}/retrieval/uploads",
        params={"actor_id": review.sam, "file_name": "x.pdf"},
        content=_scan(),
        headers={"Content-Type": "application/pdf"},
    )
    assert refused.status_code == 403

    held = review.get("retrieval/uploads")
    assert [h["upload_id"] for h in held] == [named_wrongly["upload_id"], scan["upload_id"]]
    # The lead discards the checklist and assigns the scan to the registry entry.
    review.post(f"retrieval/uploads/{named_wrongly['upload_id']}/discard", 204)
    attached = review.post(
        f"retrieval/uploads/{scan['upload_id']}/assign",
        200,
        report_id=review.reports["registry"],
    )
    assert (attached["item_ref"], attached["licence"]) == (
        review.reports["registry"],
        "CC BY 4.0",
    )
    assert review.get("retrieval/uploads") == []
    review.post(f"retrieval/uploads/{scan['upload_id']}/discard", 409)

    uploads = [
        (e.type, e.payload.get("matched_by"))
        for e in review.events(lambda t: t.startswith("retrieval.upload"))
    ]
    assert uploads == [
        ("retrieval.upload_matched", "file_name"),
        ("retrieval.upload_matched", "content"),
        ("retrieval.upload_held", None),
        ("retrieval.upload_held", None),
        ("retrieval.upload_discarded", None),
        ("retrieval.upload_assigned", None),
    ]
    assert len(review.events(lambda t: t == "project.full_text_attached")) == 3

    # Open access for the one left: the Lancet trial, which no source gives.
    review.post("retrieval/open-access")
    assert review.work() == 1
    (missing,) = review.get("retrieval/missing")
    assert (missing["doi"], missing["pmid"]) == (LANCET_DOI, "32450107")
    assert missing["file_name"] == "10.1016_s0140-6736(20)31180-6.pdf"

    exported = review.client.get(
        f"/api/projects/{review.pid}/retrieval/missing/export",
        params={"format": "csv"},
        headers={"X-Kittum-Reviewer": review.lead},
    )
    assert exported.headers["content-type"].startswith("text/csv")
    assert "missing-full-texts.csv" in exported.headers["content-disposition"]
    (row,) = list(csv.DictReader(io.StringIO(exported.text)))
    assert (row["DOI"], row["PMID"], row["Year"]) == (LANCET_DOI, "32450107", "2020")
    assert row["Title"].startswith("Hydroxychloroquine or chloroquine")
    assert row["Authors"].startswith("Mehra")
    assert row["Journal"]
    tried = row["Sources tried"].split("; ")
    assert tried == ["PubMed Central", "OpenAlex", "Semantic Scholar"]
    assert row["Why each attempt failed"] == (
        "PubMed Central has no open copy. OpenAlex (www.thelancet.com) refused the download"
        " (HTTP 403). Semantic Scholar named a file an earlier source named."
    )
    assert row["Name the PDF"] == "10.1016_s0140-6736(20)31180-6.pdf"

    ris = review.client.get(
        f"/api/projects/{review.pid}/retrieval/missing/export",
        params={"format": "ris"},
        headers={"X-Kittum-Reviewer": review.lead},
    )
    (entry,) = rispy.loads(ris.text)
    assert entry["doi"].lower() == LANCET_DOI
    assert "Kittum has no usable full text for this report." in entry["notes"]
    assert any(
        note.startswith("Sources tried: PubMed Central; OpenAlex") for note in entry["notes"]
    )

    for path in ("retrieval/missing", "retrieval/uploads", "retrieval/missing/export"):
        assert (
            review.client.get(
                f"/api/projects/{review.pid}/{path}", headers={"X-Kittum-Reviewer": review.sam}
            ).status_code
            == 403
        ), path
