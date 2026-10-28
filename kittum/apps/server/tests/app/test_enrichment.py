"""Enrichment and open-access retrieval through the worker, on recorded real responses.

A lead builds a corpus from a citation search (two CC BY trials in BMC Medicine and a registry
entry) and a guideline's reference list (a trial the Lancet retracted). Building it queues the
enrichment; the worker runs it through the real `SourceGateway` while PubTator answers 503, so
every other source fills its fields, PubTator's failure is recorded against the records it did not
answer, and the run still finishes. The lead asks again once PubTator is back: the gateway's cache
answers the other sources, and the entity tags arrive. After both reviewers include everything at
title and abstract, the lead uploads one PDF by hand and has the worker walk the open-access
waterfall for the rest: Europe PMC's PDF link is refused (a real Cloudflare 403), the publisher's
CC BY PDF from Crossref's links is acquired, parsed, and attached, and the registry entry has no
location at all. Reviewers get 403 from every route of it.

The gateway answers from `tests/enrich/fixtures/stack/`, recorded once by
`tests.app.record_stack`; nothing touches the network.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app import job_handlers, open_access
from kittum.app.demo import DATA, PDFS, PROTOCOL, SENTINELS, demo_judge
from kittum.core import FixedClock, new_id
from kittum.gateway import RecordedTransport, SourceGateway
from kittum.gateway.testing import response
from kittum.ledger import PostgresLedger
from kittum.models import ModelRegistry
from kittum.runs import PostgresJobQueue, PostgresReceipts, Worker
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.app.record_stack import GUIDELINE_REFERENCES, STACK
from tests.documents.conftest import savepoint_db
from tests.enrich.support import CONTACT_EMAIL

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.gateway import HttpRequest, HttpResponse

pytestmark = pytest.mark.postgres

LESOTHO_DOI = "10.1186/s12916-026-04943-4"
RETRACTED_DOI = "10.1016/s0140-6736(20)31180-6"
RETRACTION_NOTICE = "10.1016/s0140-6736(20)31324-6"


class Stack:
    """The recorded sources, with PubTator down until `pubtator_up` is set."""

    def __init__(self) -> None:
        self.recorded = RecordedTransport(STACK)
        self.pubtator_up = False
        self.sent: list[str] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request.url)
        if "pubtator3-api" in request.url and not self.pubtator_up:
            return response(503, b"Service Unavailable")
        return self.recorded.send(request)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _include_everything(client: TestClient, pid: str, reviewer: str) -> int:
    as_ = {"X-Kittum-Reviewer": reviewer}
    params = {"scope_id": "nrt-vs-control", "stage": "title_abstract", "limit": 50}
    decided = 0
    for item in client.get(
        f"/api/projects/{pid}/screening/queue", params=params, headers=as_
    ).json():
        body: dict[str, Any] = {
            "command_id": str(new_id()),
            "decision": {"outcome": "include"},
            "phase": "provisional",
            "expected_etag": item["etag"],
            "timing": {"shown_at": _now(), "decided_at": _now()},
        }
        url = f"/api/projects/{pid}/screening/assignments/{item['assignment_id']}/decide"
        result = client.post(url, json=body, headers=as_)
        assert result.status_code == 200, result.text
        if result.json()["phase"] == "provisional":  # the AI was revealed; keep the decision
            body |= {
                "command_id": str(new_id()),
                "phase": "final",
                "expected_etag": result.json()["etag"],
            }
            assert client.post(url, json=body, headers=as_).status_code == 200
        decided += 1
    return decided


def test_a_lead_enriches_the_corpus_and_fetches_open_access_full_texts(
    pg: Tx, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = Stack()
    gateway_clock = FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    db = savepoint_db(pg)
    artifacts = MemoryStore()
    ledger = PostgresLedger()
    ctx = AppContext(
        settings=Settings(
            database_url="unused", artifact_dir=Path("unused"), contact_email=CONTACT_EMAIL
        ),
        db=db,
        artifacts=artifacts,
        # The gateway's waits (for a rate-limit token, a cooldown) pass on a clock of the test's
        # own, so they take no wall time and never depend on how fast the machine is: run 1 spends
        # PubTator's two tokens for the second, and run 2 waits for the next one, however soon it
        # starts.
        gateway=SourceGateway(
            database=db,
            transport=stack,
            artifacts=artifacts,
            buckets="memory",
            max_attempts=2,
            clock=gateway_clock,
            sleep=lambda seconds: gateway_clock.advance(timedelta(seconds=seconds)),
        ),
        ledger=ledger,
        queue=PostgresJobQueue(),
        receipts=PostgresReceipts(),
        judge=demo_judge(ModelRegistry.load()),
    )
    # Only this project's jobs: the shared database may hold others' (rolled back with the test).
    pg.execute(
        "update job set run_after = now() + interval '1 day' where status in ('queued', 'running')"
    )
    worker = Worker(db, ctx.queue, job_handlers(ctx), name="enrich-test", lease=timedelta(hours=1))  # type: ignore[arg-type]

    def work() -> int:
        ran = 0
        while worker.run_once():
            ran += 1
        return ran

    client = TestClient(create_app(ctx))

    def ok(answer: Any, status: int = 200) -> Any:
        assert answer.status_code == status, answer.text
        return answer.json()

    project = ok(client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan Lee"}), 201)
    pid = project["id"]
    lead = project["members"][0]["actor_id"]
    as_lead = {"X-Kittum-Reviewer": lead}
    people = {}
    for name, role in (
        ("Sam", "reviewer"),
        ("Jun", "reviewer"),
        ("Rita", "auditor"),
        ("Elena", "adjudicator"),
    ):
        body = {"display_name": name, "roles": [role], "added_by": lead}
        people[name] = ok(client.post(f"/api/projects/{pid}/members", json=body), 201)["actor_id"]
    as_sam = {"X-Kittum-Reviewer": people["Sam"]}
    source = yaml.safe_load(PROTOCOL.read_text())
    version = ok(client.post(f"/api/projects/{pid}/protocol-versions", json=source), 201)
    ok(
        client.post(
            f"/api/projects/{pid}/protocol-versions/{version['id']}/publish",
            json={"approved_by": lead},
        )
    )
    for source, path in (
        ("citation_search", DATA / "citation_search.ris"),
        ("guideline_references", GUIDELINE_REFERENCES),
    ):
        plan = {"topic_id": "TRIALS", "source": source, "platform": source, "strategy": "x"}
        body = {"plan": plan, "data": path.read_text(), "format": "ris", "actor_id": lead}
        ok(client.post(f"/api/projects/{pid}/search/import", json=body))

    # Building the corpus queues its enrichment; the worker runs it with PubTator down.
    ok(client.post(f"/api/projects/{pid}/corpus/ingest", json={"actor_id": lead}), 201)
    queued = ok(client.get(f"/api/projects/{pid}/enrichment/runs/latest", headers=as_lead))
    assert (queued["status"], queued["trigger"], queued["records"], queued["done"]) == (
        "queued",
        "corpus_built",
        4,
        0,
    )
    assert work() == 1
    run = ok(client.get(f"/api/projects/{pid}/enrichment/runs/latest", headers=as_lead))
    assert (run["status"], run["done"], run["retracted"]) == ("done", 4, 1)
    sources = {s["source"]: s for s in run["sources"]}
    # Three records have a DOI; the registry entry has no identifier to look up.
    assert {k: (v["asked"], v["answered"], v["failed"]) for k, v in sources.items()} == {
        "openalex": (3, 3, 0),
        "europepmc": (3, 3, 0),
        "crossref": (3, 3, 0),
        "unpaywall": (3, 3, 0),
        "pubtator": (3, 0, 3),
    }
    assert "503" in sources["pubtator"]["last_error"]

    corpus = ok(client.get(f"/api/projects/{pid}/corpus/revisions/latest", headers=as_lead))
    occurrence = {
        r["records"][0]["doi"] or r["records"][0]["title"]: r["records"][0]["occurrence_id"]
        for r in corpus["reports"]
    }
    report_of = {
        r["records"][0]["doi"] or r["records"][0]["title"]: r["report_id"]
        for r in corpus["reports"]
    }
    retracted_id = next(v for k, v in occurrence.items() if k.lower() == RETRACTED_DOI)
    detail = ok(
        client.get(f"/api/projects/{pid}/enrichment/records/{retracted_id}", headers=as_lead)
    )
    # The retraction comes from Crossref's `updated-by`, the record's own DOI from its search,
    # and the PMID and abstract the reference list lacked from the first source that had them.
    assert detail["retracted"] is True
    assert detail["retraction_notice_doi"] == RETRACTION_NOTICE
    assert {"doi": RETRACTION_NOTICE, "kind": "retraction", "direction": "updated_by"} in detail[
        "updates"
    ]
    provenance = {(f["field"], f["source"]) for f in detail["field_sources"]}
    assert {
        ("doi", "guideline_references"),
        ("pmid", "openalex"),
        ("retraction", "crossref"),
    } <= provenance
    assert detail["pmid"] == "32450107"
    assert detail["abstract"] and ("abstract", "europepmc") in provenance  # OpenAlex has none
    assert detail["entities"] == []
    assert [f["source"] for f in detail["failures"]] == ["pubtator"]
    flags = {
        f["occurrence_id"]: f
        for f in ok(client.get(f"/api/projects/{pid}/enrichment/records", headers=as_lead))
    }
    assert flags[retracted_id]["retracted"] is True
    assert "expression_of_concern" in flags[retracted_id]["updates"]
    lesotho = occurrence[LESOTHO_DOI]
    assert flags[lesotho]["open_access"] is True and flags[lesotho]["retracted"] is False

    # Asked again with PubTator back: only PubTator is fetched; the cache answers the rest.
    before = len(stack.sent)
    stack.pubtator_up = True
    again = ok(client.post(f"/api/projects/{pid}/enrichment/runs", json={"actor_id": lead}), 202)
    assert again["id"] != run["id"] and again["trigger"] == "requested"
    assert work() == 1
    assert all("pubtator3-api" in url for url in stack.sent[before:]) and stack.sent[before:]
    rerun = ok(client.get(f"/api/projects/{pid}/enrichment/runs/latest", headers=as_lead))
    assert {s["source"]: s["failed"] for s in rerun["sources"]}["pubtator"] == 0
    tagged = ok(client.get(f"/api/projects/{pid}/enrichment/records/{lesotho}", headers=as_lead))
    assert tagged["failures"] == []
    text = f"{tagged['title']} {tagged['abstract']}"
    tags = {text[e["start"] : e["end"]]: e["label"] for e in tagged["entities"]}
    assert tags.get("metformin") == "drug" and tags.get("type 2 diabetes") == "disease"
    assert ("entities", "pubtator") in {(f["field"], f["source"]) for f in tagged["field_sources"]}
    oa = {(o["source"], o["format"]) for o in tagged["oa_locations"]}
    assert {("europepmc", "pdf"), ("crossref", "pdf")} <= oa

    # The ledger names each field's source per record.
    with db.transaction() as tx:
        events = [e for e in ledger.events(tx, pid) if e.type.startswith("enrichment.")]
    assert [e.type for e in events] == [
        "enrichment.requested",
        "enrichment.records_enriched",
        "enrichment.finished",
    ] * 2
    first = events[1].payload["records"]
    assert ["retraction", "crossref"] in next(
        r["field_sources"] for r in first if r["occurrence_id"] == str(retracted_id)
    )

    # Screening: both reviewers include everything at title and abstract.
    stage = {"scope_id": "nrt-vs-control", "stage": "title_abstract", "actor_id": lead}
    sentinels = [s.model_dump(mode="json") for s in SENTINELS]
    ok(
        client.post(f"/api/projects/{pid}/screening/stages", json=stage | {"sentinels": sentinels}),
        201,
    )
    work()  # the AI judge reads every record
    for name in ("Sam", "Jun"):
        assert _include_everything(client, pid, people[name]) >= 4

    # Nothing of it reaches a reviewer: not the runs, the records, nor the waterfall.
    for method, path in (
        ("get", "enrichment/runs/latest"),
        ("get", "enrichment/records"),
        ("get", f"enrichment/records/{lesotho}"),
        ("get", "retrieval/open-access/latest"),
        ("get", f"retrieval/reports/{report_of[LESOTHO_DOI]}/attempts"),
    ):
        assert (
            client.request(method, f"/api/projects/{pid}/{path}", headers=as_sam).status_code == 403
        ), path
    for path in ("enrichment/runs", "retrieval/open-access"):
        refused = client.post(f"/api/projects/{pid}/{path}", json={"actor_id": people["Sam"]})
        assert refused.status_code == 403, path
    # The ledger tells a reviewer that it holds, not how many events of which type it has.
    theirs = ok(client.get(f"/api/projects/{pid}/ledger/verify", headers=as_sam))
    assert theirs["ok"] and (theirs["by_type"], theirs["events"], theirs["verify"]) == (
        None,
        None,
        None,
    )

    # The lead uploads one trial by hand, then fetches the rest from open access.
    vitamin_c = report_of["10.1186/s12916-026-05037-x"]
    ok(
        client.post(
            f"/api/projects/{pid}/full-texts/{vitamin_c}",
            params={"actor_id": lead, "licence": "CC BY 4.0"},
            content=(PDFS / "li2026-vitamin-c-kidney-biopsy-rct.pdf").read_bytes(),
            headers={"Content-Type": "application/pdf"},
        ),
        201,
    )
    fetch = ok(
        client.post(f"/api/projects/{pid}/retrieval/open-access", json={"actor_id": lead}), 202
    )
    assert fetch["status"] == "queued"
    assert len(fetch["reports"]) == 3 and {r["outcome"] for r in fetch["reports"]} == {None}

    # The first attach fails (a full disk, say). The attempts before it stay on record, each
    # committed with its ledger event, and the retried job goes on from there.
    def full_disk(*_args: object, **_kwargs: object) -> None:
        raise OSError("No space left on device")

    real_attach = open_access.attach_acquired_full_text
    monkeypatch.setattr(open_access, "attach_acquired_full_text", full_disk)
    assert work() == 1
    stuck = ok(client.get(f"/api/projects/{pid}/retrieval/open-access/latest", headers=as_lead))
    (first,) = [r for r in stuck["reports"] if r["report_id"] == report_of[LESOTHO_DOI]]
    assert first["outcome"] is None
    assert [a["outcome"] for a in first["attempts"]] == ["http_error", "acquired"]
    with db.transaction() as tx:
        kept = [
            (e.type, (e.payload.get("attempt") or e.payload)["url"])
            for e in ledger.events(tx, pid)
            if e.type.startswith("document.")
        ]
    assert [k for k in kept if "PMC13374172" in k[1] or "04943-4" in k[1]] == [
        ("document.acquisition_attempted", first["attempts"][0]["url"]),
        ("document.acquired", first["attempts"][1]["url"]),
    ]
    monkeypatch.setattr(open_access, "attach_acquired_full_text", real_attach)
    pg.execute(
        "update job set run_after = now() where project_id = %s and status = 'queued'", (pid,)
    )
    before = len(stack.sent)
    assert work() == 1
    # Resumed after the last recorded attempt: nothing for the Lesotho trial is fetched again.
    assert not [u for u in stack.sent[before:] if "04943-4" in u or "PMC13374172" in u]
    done = ok(client.get(f"/api/projects/{pid}/retrieval/open-access/latest", headers=as_lead))
    assert done["status"] == "done"
    by_report = {r["report_id"]: r for r in done["reports"]}
    acquired = by_report[report_of[LESOTHO_DOI]]
    assert acquired["outcome"] == "acquired"
    assert [
        (a["source"], a["host"], a["status"], a["outcome"], a["licence"])
        for a in acquired["attempts"]
    ] == [
        ("europepmc", "europepmc.org", 403, "http_error", "cc by"),
        ("crossref", "link.springer.com", 200, "acquired", "cc-by-4.0"),
    ]
    assert (acquired["source"], acquired["licence"], acquired["page_count"]) == (
        "crossref",
        "cc-by-4.0",
        13,
    )
    registry = by_report[report_of[next(k for k in report_of if "ComBaCaL" in k)]]
    assert (registry["outcome"], registry["attempts"]) == ("no_location", [])
    lancet = by_report[report_of[next(k for k in report_of if k.lower() == RETRACTED_DOI)]]
    assert lancet["outcome"] == "not_acquired"
    assert {a["outcome"] for a in lancet["attempts"]} <= {"http_error", "not_pdf"}
    assert (
        ok(
            client.get(
                f"/api/projects/{pid}/retrieval/reports/{report_of[LESOTHO_DOI]}/attempts",
                headers=as_lead,
            )
        )
        == acquired["attempts"]
    )

    # The acquired PDF is the report's full text, parsed, beside the one uploaded by hand.
    texts = {
        t["item_ref"]: t["full_text"]
        for t in ok(client.get(f"/api/projects/{pid}/full-texts", headers=as_lead))
    }
    assert texts[report_of[LESOTHO_DOI]]["licence"] == "cc-by-4.0"
    assert texts[report_of[LESOTHO_DOI]]["layer_id"]
    assert texts[vitamin_c]["licence"] == "CC BY 4.0"
    with db.transaction() as tx:
        events = ledger.events(tx, pid)
    attached = [e for e in events if e.type == "project.full_text_attached"]
    assert [e.payload.get("source") for e in attached] == [None, "crossref"]
    # One event per attempt, each committed before the next was tried.
    (got,) = [e for e in events if e.type == "document.acquired"]
    assert [a["outcome"] for a in got.payload["attempts"]] == ["acquired"]
    refused = [
        e.payload["attempt"]["url"] for e in events if e.type == "document.acquisition_attempted"
    ]
    assert sorted(refused) == sorted(
        [a["url"] for a in acquired["attempts"][:1]] + [a["url"] for a in lancet["attempts"]]
    )
    assert [e.type for e in events if e.type.startswith("retrieval.")] == [
        "retrieval.open_access_requested",
        "retrieval.no_open_access_location",
        "retrieval.open_access_finished",
    ]

    # Each report's audit trail holds its own retrieval: the request, every attempt, failed or
    # not, and the attach; and none of another report's attempts.
    def trail(report: str) -> list[tuple[str, str]]:
        found = ok(client.get(f"/api/projects/{pid}/audit/records/{report}/trace", headers=as_lead))
        return [
            (e["type"], e["summary"])
            for e in found["events"]
            if e["type"].startswith(("retrieval.", "document.", "project.full_text"))
        ]

    assert trail(report_of[LESOTHO_DOI]) == [
        (
            "retrieval.open_access_requested",
            "Morgan Lee asked Kittum to look for open-access full texts of 3 reports",
        ),
        (
            "document.acquisition_attempted",
            "Kittum could not get the full text from europepmc (http error)",
        ),
        ("document.acquired", "Kittum fetched the full text from crossref"),
        (
            "project.full_text_attached",
            "Kittum attached the full text it fetched from crossref",
        ),
    ]
    assert trail(lancet["report_id"]) == [
        (
            "retrieval.open_access_requested",
            "Morgan Lee asked Kittum to look for open-access full texts of 3 reports",
        ),
        *[
            (
                "document.acquisition_attempted",
                f"Kittum could not get the full text from {a['source']}"
                f" ({a['outcome'].replace('_', ' ')})",
            )
            for a in lancet["attempts"]
        ],
    ]
    assert trail(registry["report_id"]) == [
        (
            "retrieval.open_access_requested",
            "Morgan Lee asked Kittum to look for open-access full texts of 3 reports",
        ),
        ("retrieval.no_open_access_location", "Kittum found no open-access copy of the full text"),
    ]

    # Every report sought has its full text or a recorded reason why not: a second run has only
    # the two without one to try again.
    second = ok(
        client.post(f"/api/projects/{pid}/retrieval/open-access", json={"actor_id": lead}), 202
    )
    assert {r["report_id"] for r in second["reports"]} == {
        registry["report_id"],
        lancet["report_id"],
    }
