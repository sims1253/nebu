"""Full-text screening over HTTP: one reason per decision, and the reviewer's "wrong document".

A report is excluded on the one criterion it fails, with evidence, and included in one action
that says every criterion is met. A PDF that is not the record's report (here a reporting
checklist stored as a cohort study's full text, as in issue #60) is marked as the wrong
document: the report leaves every queue, the record has no full text again and is listed as
missing, PRISMA counts it as not retrieved and never as excluded, the ledger says who said so and
why, and the record can get its real full text. Runs on a rolled-back connection, with the
recorded judge in a worker on the same connection.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pymupdf
import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app import job_handlers
from kittum.app.demo import DATA, PDFS, PROTOCOL, demo_judge
from kittum.core import new_id
from kittum.ledger import PostgresLedger
from kittum.models import ModelRegistry
from kittum.runs import PostgresJobQueue, PostgresReceipts, Worker
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.documents.conftest import savepoint_db

if TYPE_CHECKING:
    from kittum.db import Tx

pytestmark = pytest.mark.postgres

SENTINELS = PROTOCOL.parents[1] / "sentinels" / "nicotine-replacement.yaml"
LESOTHO = PDFS / "gerber2026-lesotho-t2d-crct.pdf"
VITAMIN_C = PDFS / "li2026-vitamin-c-kidney-biopsy-rct.pdf"
TRIAL = "Community health worker-led"
COHORT = "Community-Based Chronic Disease Care"
SCOPE = "nrt-vs-control"
OTHER = "combination-vs-single"  # the topic's other scope: the same records, its own stages
NOTE = "This is the journal's CONSORT-EHEALTH checklist, not the cohort study."


def checklist_pdf() -> bytes:
    """A reporting checklist of the kind open-access sources list as a work's PDF."""
    doc = pymupdf.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            f"CONSORT-EHEALTH checklist V1.6.1, page {n + 1}\n"
            "1a-i) Identify the mode of delivery in the title\n"
            "1b) Structured summary of trial design, methods, results, and conclusions",
        )
    return doc.tobytes()


def test_one_reason_per_decision_and_a_wrong_document_is_missing_not_excluded(pg: Tx) -> None:
    queue = PostgresJobQueue()
    ctx = AppContext(
        settings=Settings(database_url="postgresql://unused", artifact_dir=Path("unused")),
        db=savepoint_db(pg),
        artifacts=MemoryStore(),
        ledger=PostgresLedger(),
        judge=demo_judge(ModelRegistry.load()),
        queue=queue,
        receipts=PostgresReceipts(),
    )
    client = TestClient(create_app(ctx))
    worker = Worker(ctx.db, queue, job_handlers(ctx), name="ft", lease=timedelta(minutes=30))

    def ok(response: Any, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    def work() -> None:
        while worker.run_once():
            pass

    project = ok(client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan"}), 201)
    pid = project["id"]
    lead = project["members"][0]["actor_id"]
    people = {"Morgan": lead}
    for name, role in (
        ("Sam", "reviewer"),
        ("Jun", "reviewer"),
        ("Rita", "auditor"),
        ("Elena", "adjudicator"),
    ):
        body = {"display_name": name, "roles": [role], "added_by": lead}
        people[name] = ok(client.post(f"/api/projects/{pid}/members", json=body), 201)["actor_id"]
    source = ok(client.post("/api/protocol/parse", json={"text": PROTOCOL.read_text()}))["source"]
    draft = ok(client.post(f"/api/projects/{pid}/protocol-versions", json=source), 201)
    ok(
        client.post(
            f"/api/projects/{pid}/protocol-versions/{draft['id']}/publish",
            json={"approved_by": lead},
        )
    )
    plan = {"topic_id": "TRIALS", "source": "citation_search", "platform": "Lists", "strategy": "x"}
    body = {"plan": plan, "data": (DATA / "citation_search.ris").read_text(), "format": "ris"}
    ok(client.post(f"/api/projects/{pid}/search/import", json=body | {"actor_id": lead}))
    ok(client.post(f"/api/projects/{pid}/corpus/ingest", json={"actor_id": lead}), 201)
    sentinels = ok(
        client.post(
            f"/api/projects/{pid}/screening/sentinels/parse", json={"text": SENTINELS.read_text()}
        )
    )
    for scope in (SCOPE, OTHER):
        stage = {"scope_id": scope, "stage": "title_abstract", "actor_id": lead}
        ok(
            client.post(
                f"/api/projects/{pid}/screening/stages", json=stage | {"sentinels": sentinels}
            ),
            201,
        )
    work()

    def as_(person: str) -> dict[str, str]:
        return {"X-Kittum-Reviewer": people[person]}

    def queue_of(person: str, stage: str, scope: str = SCOPE) -> list[dict[str, Any]]:
        params = {"scope_id": scope, "stage": stage, "limit": 50}
        return ok(
            client.get(f"/api/projects/{pid}/screening/queue", params=params, headers=as_(person))
        )

    def decide(person: str, item: dict[str, Any], decision: dict[str, Any]) -> Any:
        """A provisional decision, kept as the final one if the AI's reading is shown."""
        now = datetime.now(UTC).isoformat()
        timing = {"shown_at": now, "first_key_at": now, "decided_at": now, "reveal_shown_at": None}
        path = f"/api/projects/{pid}/screening/assignments/{item['assignment_id']}/decide"
        payload = {"decision": decision, "timing": timing, "expected_etag": item["etag"]}
        first = client.post(
            path,
            json=payload | {"command_id": str(new_id()), "phase": "provisional"},
            headers=as_(person),
        )
        if first.status_code != 200 or first.json()["phase"] == "final":
            return first
        final = payload | {"expected_etag": first.json()["etag"], "command_id": str(new_id())}
        ok(client.post(path, json=final | {"phase": "final"}, headers=as_(person)))
        return first

    def outcome(name: str, reason: str | None = None, criteria: Any = (), anchors: Any = ()) -> Any:
        return {
            "outcome": name,
            "reason_criterion_id": reason,
            "evidence_anchor_ids": list(anchors),
            "comment": None,
            "criteria": list(criteria),
        }

    for scope in (SCOPE, OTHER):
        for person in ("Sam", "Jun", "Rita"):
            for item in queue_of(person, "title_abstract", scope):
                ok(decide(person, item, outcome("include")))

    reports = ok(client.get(f"/api/projects/{pid}/full-texts", headers=as_("Morgan")))
    trial = next(r for r in reports if r["title"].startswith(TRIAL))
    cohort = next(r for r in reports if r["title"].startswith(COHORT))
    pdf = {"headers": {"Content-Type": "application/pdf"}}

    def attach(ref: str, data: bytes, status: int = 201) -> Any:
        return ok(
            client.post(
                f"/api/projects/{pid}/full-texts/{ref}",
                params={"actor_id": lead},
                content=data,
                **pdf,
            ),
            status,
        )

    attach(trial["item_ref"], LESOTHO.read_bytes())
    wrong = attach(cohort["item_ref"], checklist_pdf())
    ok(
        client.post(
            f"/api/projects/{pid}/full-text-sentinels",
            params={"actor_id": lead, "title": "Vitamin C", "expected_outcome": "exclude"},
            content=VITAMIN_C.read_bytes(),
            **pdf,
        ),
        201,
    )
    for scope in (SCOPE, OTHER):
        ft = {"scope_id": scope, "stage": "full_text", "actor_id": lead}
        ok(client.post(f"/api/projects/{pid}/screening/stages", json=ft), 201)
    work()

    def wrong_document(person: str, item: dict[str, Any], etag: str | None = None) -> Any:
        """Mark the PDF of the person's assignment as the wrong document."""
        base = f"/api/projects/{pid}/screening/assignments/{item['assignment_id']}"
        shown = ok(client.get(f"{base}/full-text", headers=as_(person)))
        body = {
            "command_id": str(new_id()),
            "expected_etag": etag or item["etag"],
            "document_id": shown["document_id"],
        }
        return client.post(f"{base}/wrong-document", json=body, headers=as_(person))

    def titles(person: str, scope: str = SCOPE) -> list[str]:
        return [i["title"] for i in queue_of(person, "full_text", scope)]

    sam_other_trial = next(
        i for i in queue_of("Sam", "full_text", OTHER) if i["title"].startswith(TRIAL)
    )
    assert any(t.startswith(COHORT) for t in titles("Jun", OTHER))

    def status_of(person: str) -> Any:
        params = {"scope_id": SCOPE, "stage": "full_text"}
        return ok(
            client.get(f"/api/projects/{pid}/screening/status", params=params, headers=as_(person))
        )

    sam_items = {i["title"]: i for i in queue_of("Sam", "full_text")}
    jun_items = {i["title"]: i for i in queue_of("Jun", "full_text")}
    assert len(sam_items) == len(jun_items) == 3  # the trial, the cohort study, a sentinel
    jun_before = status_of("Jun")["remaining"]
    sam_cohort = next(i for t, i in sam_items.items() if t.startswith(COHORT))
    jun_cohort = next(i for t, i in jun_items.items() if t.startswith(COHORT))
    shown = ok(
        client.get(
            f"/api/projects/{pid}/screening/assignments/{sam_cohort['assignment_id']}/full-text",
            headers=as_("Sam"),
        )
    )
    assert shown["document_id"] == wrong["document_id"]
    assert shown["ai_quote"] is None  # nothing of the AI before Sam decides

    # Sam sees a checklist where the cohort study should be, and says so.
    path = f"/api/projects/{pid}/screening/assignments/{sam_cohort['assignment_id']}/wrong-document"
    command = {
        "command_id": str(new_id()),
        "expected_etag": sam_cohort["etag"],
        "document_id": shown["document_id"],
        "note": NOTE,
    }
    trial_assignment = next(i for t, i in sam_items.items() if t.startswith(TRIAL))
    mismatched = client.post(
        f"/api/projects/{pid}/screening/assignments/{trial_assignment['assignment_id']}/wrong-document",
        json=command | {"expected_etag": trial_assignment["etag"]},
        headers=as_("Sam"),
    )
    assert mismatched.status_code == 409  # not the PDF of that assignment
    marked = ok(client.post(path, json=command, headers=as_("Sam")))
    head = len(ctx.require_ledger().events(pg, UUID(pid)))
    assert ok(client.post(path, json=command, headers=as_("Sam"))) == marked  # a retry: no-op
    assert len(ctx.require_ledger().events(pg, UUID(pid))) == head

    # The report left both screeners' queues, in both scopes: the PDF was the record's, not a
    # scope's. Jun, who still held it, can no longer decide it.
    for scope in (SCOPE, OTHER):
        for person in ("Sam", "Jun"):
            assert not any(t.startswith(COHORT) for t in titles(person, scope)), (person, scope)
    assert status_of("Jun")["remaining"] == jun_before - 1
    stale = decide("Jun", jun_cohort, outcome("include"))
    assert stale.status_code == 409

    # The record has no full text again: the lead's retrieval list shows it as missing.
    reports = ok(client.get(f"/api/projects/{pid}/full-texts", headers=as_("Morgan")))
    assert next(r for r in reports if r["item_ref"] == cohort["item_ref"])["full_text"] is None
    assert next(r for r in reports if r["item_ref"] == trial["item_ref"])["full_text"] is not None

    # Jun excludes the trial on the one criterion it fails, with evidence for that criterion only.
    jun_trial = next(i for i in queue_of("Jun", "full_text") if i["title"].startswith(TRIAL))
    source = ok(
        client.get(
            f"/api/projects/{pid}/screening/assignments/{jun_trial['assignment_id']}/full-text",
            headers=as_("Jun"),
        )
    )
    pop = next(c for c in source["mentions"] if c["criterion_id"] == "POP")
    mention = pop["mentions"][0]
    no_evidence = decide(
        "Jun",
        jun_trial,
        outcome("exclude", "POP", [{"criterion_id": "POP", "status": "not_met"}]),
    )
    assert no_evidence.status_code == 422
    assert "adopt evidence" in no_evidence.json()["detail"][0]["msg"]
    anchor = ok(
        client.post(
            f"/api/layers/{source['layer_id']}/anchors",
            json={"quote": mention["quote"]["exact"], "start": mention["start"]},
        ),
        201,
    )
    reading = {"criterion_id": "POP", "status": "not_met", "evidence_anchor_ids": [anchor["id"]]}
    excluded = outcome("exclude", "POP", [reading], [anchor["id"]])
    now = datetime.now(UTC).isoformat()
    timing = {"shown_at": now, "first_key_at": now, "decided_at": now, "reveal_shown_at": None}
    decide_path = f"/api/projects/{pid}/screening/assignments/{jun_trial['assignment_id']}/decide"
    first = ok(
        client.post(
            decide_path,
            json={
                "command_id": str(new_id()),
                "decision": excluded,
                "phase": "provisional",
                "expected_etag": jun_trial["etag"],
                "timing": timing,
            },
            headers=as_("Jun"),
        )
    )
    # The model saw no failing criterion, so Jun's exclusion shows its reading of the population.
    # It quoted nothing, so nothing is placed on the page.
    assert first["phase"] == "provisional"
    assert first["reveal"]["criterion_id"] == "POP"
    assert first["reveal"]["quote_span"] is None
    # Having seen the AI disagree, Jun cannot take the report out instead of deciding.
    escaped = wrong_document("Jun", jun_trial, first["etag"])
    assert escaped.status_code == 422
    assert "You decided this report already" in escaped.json()["detail"][0]["msg"]
    ok(
        client.post(
            decide_path,
            json={
                "command_id": str(new_id()),
                "decision": excluded,
                "phase": "final",
                "expected_etag": first["etag"],
                "timing": timing | {"reveal_shown_at": now},
            },
            headers=as_("Jun"),
        )
    )
    revealed = ok(
        client.get(
            f"/api/projects/{pid}/screening/assignments/{jun_trial['assignment_id']}/full-text",
            headers=as_("Jun"),
        )
    )
    assert revealed["ai_quote"] is None

    # Once someone has decided a report, nobody can take it out of screening: not Sam, who has
    # not decided it yet, in this scope or in the other.
    sam_trial = next(i for i in queue_of("Sam", "full_text") if i["title"].startswith(TRIAL))
    for person, item in (("Sam", sam_trial), ("Sam", sam_other_trial)):
        refused = wrong_document(person, item)
        assert refused.status_code == 422, (person, refused.text)
        assert "Someone already decided this report" in refused.json()["detail"][0]["msg"]

    # Sam includes the trial in one action: every criterion met, no verdict on each, no evidence.
    ok(decide("Sam", sam_trial, outcome("include")))

    for person in ("Sam", "Jun", "Rita"):
        for item in queue_of(person, "full_text"):
            ok(decide(person, item, outcome("include")))

    # PRISMA: the cohort study is a report not retrieved, never an exclusion. The trial is in
    # conflict between Sam and Jun. The flow's own checks hold.
    flows = ok(client.get(f"/api/projects/{pid}/audit/as-of", headers=as_("Morgan")))["flows"]
    # The other scope too: there, the cohort study is not retrieved either.
    (other,) = [f for f in flows if f["scope_id"] == OTHER]
    assert cohort["item_ref"] in other["members"]["reports_not_retrieved"]
    assert cohort["item_ref"] not in other["members"]["reports_assessed"]
    assert all(c["holds"] for c in other["checks"]), other["checks"]
    (flow,) = [f for f in flows if f["scope_id"] == SCOPE]
    members = flow["members"]
    assert cohort["item_ref"] in members["reports_not_retrieved"]
    assert cohort["item_ref"] not in members["reports_excluded"]
    assert cohort["item_ref"] not in members["reports_assessed"]
    assert flow["reports_excluded"] == 0
    assert members["reports_unresolved"] == [trial["item_ref"]]
    assert all(c["holds"] for c in flow["checks"]), flow["checks"]

    # The lead's figures count only what is still screened: the trial, and the sentinel.
    lead_view = status_of("Morgan")
    assert lead_view["view"] == "lead"
    (human,) = [p for p in lead_view["metrics"]["agreement"] if p["kind"] == "human_human"]
    assert human["items"] == 1

    # The record's trace says who marked the PDF, why, and that it is not an exclusion.
    traced = ok(
        client.get(
            f"/api/projects/{pid}/audit/records/{cohort['item_ref']}/trace", headers=as_("Morgan")
        )
    )
    summaries = [e["summary"] for e in traced["events"]]
    assert (
        "Sam marked the PDF as the wrong document. The report left full-text screening without a decision. This is not an exclusion."
        in summaries
    )
    detached = next(e for e in traced["events"] if e["summary"].startswith("Sam detached"))
    assert {"label": "Note", "value": NOTE} in detached["facts"]

    # The lead can now give the record its real full text.
    attach(cohort["item_ref"], LESOTHO.read_bytes())
