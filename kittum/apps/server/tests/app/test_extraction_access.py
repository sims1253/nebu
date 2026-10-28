"""Who may request extraction and who may see it, on a review screened to full text over HTTP.

A run exists only for a report included at full text, so its existence tells how the screeners
decided and that the report is no sentinel. The methods lead alone requests runs, and only on a
report included at full text with a layer of its own full text. Every extraction read and
acceptance then shows the run only to a lead who screens nothing or to a member with no undecided
screening assignment for the report: here the auditor, whose audit sample at full text is the
trial, sees nothing of it until she has screened it. Runs on a rolled-back connection, with the
recorded judge and the Janus path in a worker on the same connection.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
from tests.extraction.support import schema

if TYPE_CHECKING:
    from kittum.db import Tx

pytestmark = pytest.mark.postgres

SENTINELS = PROTOCOL.parents[1] / "sentinels" / "nicotine-replacement.yaml"
LESOTHO = PDFS / "gerber2026-lesotho-t2d-crct.pdf"
VITAMIN_C = PDFS / "li2026-vitamin-c-kidney-biopsy-rct.pdf"
TRIAL_DOI = "10.1186/s12916-026-04943-4"
SCOPE = "nrt-vs-control"


def test_only_the_lead_requests_runs_on_included_reports_and_screeners_see_them_after(
    pg: Tx,
) -> None:
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
    worker = Worker(ctx.db, queue, job_handlers(ctx), name="access", lease=timedelta(minutes=30))

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
        ("Ola", "reviewer"),  # a third reviewer, whom M1 does not need: she screens nothing
    ):
        body = {"display_name": name, "roles": [role], "added_by": lead}
        people[name] = ok(client.post(f"/api/projects/{pid}/members", json=body), 201)["actor_id"]
    stranger = str(new_id())
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
    stage = {"scope_id": SCOPE, "stage": "title_abstract", "actor_id": lead}
    ok(
        client.post(f"/api/projects/{pid}/screening/stages", json=stage | {"sentinels": sentinels}),
        201,
    )
    work()

    def screen(person: str, stage: str, decision: Any) -> int:
        """Decide everything in the person's queue at `stage`; keep the decision on a reveal."""
        headers = {"X-Kittum-Reviewer": people[person]}
        params = {"scope_id": SCOPE, "stage": stage, "limit": 50}
        items = ok(
            client.get(f"/api/projects/{pid}/screening/queue", params=params, headers=headers)
        )
        for item in items:
            now = datetime.now(UTC).isoformat()
            timing = {"shown_at": now, "first_key_at": now, "decided_at": now}
            path = f"/api/projects/{pid}/screening/assignments/{item['assignment_id']}/decide"
            payload = {
                "decision": decision(item, headers),
                "timing": timing | {"reveal_shown_at": None},
                "expected_etag": item["etag"],
            }
            result = ok(
                client.post(
                    path,
                    json=payload | {"command_id": str(new_id()), "phase": "provisional"},
                    headers=headers,
                )
            )
            if result["phase"] == "provisional":  # the AI read it otherwise: keep my decision
                final = payload | {
                    "expected_etag": result["etag"],
                    "timing": timing | {"reveal_shown_at": now},
                }
                ok(
                    client.post(
                        path,
                        json=final | {"command_id": str(new_id()), "phase": "final"},
                        headers=headers,
                    )
                )
        return len(items)

    def include_ta(_item: Any, _headers: Any) -> dict[str, Any]:
        return {
            "outcome": "include",
            "reason_criterion_id": None,
            "evidence_anchor_ids": [],
            "comment": None,
            "criteria": [],
        }

    def include_ft(item: Any, headers: Any) -> dict[str, Any]:
        """Every criterion met, on the first protocol-term mention adopted as evidence."""
        source = ok(
            client.get(
                f"/api/projects/{pid}/screening/assignments/{item['assignment_id']}/full-text",
                headers=headers,
            )
        )
        mention = next(a for c in source["mentions"] for a in c["mentions"])
        anchor = ok(
            client.post(
                f"/api/layers/{source['layer_id']}/anchors",
                json={"quote": mention["quote"]["exact"], "start": mention["start"]},
            ),
            201,
        )
        return {
            "outcome": "include",
            "reason_criterion_id": None,
            "evidence_anchor_ids": [anchor["id"]],
            "comment": None,
            "criteria": [
                {
                    "criterion_id": c["criterion_id"],
                    "status": "met",
                    "evidence_anchor_ids": [anchor["id"]],
                }
                for c in source["mentions"]
            ],
        }

    for person in ("Sam", "Jun"):
        assert screen(person, "title_abstract", include_ta) == 4  # three reports, one sentinel
    reports = ok(client.get(f"/api/projects/{pid}/full-texts", headers={"X-Kittum-Reviewer": lead}))
    trial = next(r for r in reports if r["doi"] == TRIAL_DOI)
    pdf = {"headers": {"Content-Type": "application/pdf"}}
    text = ok(
        client.post(
            f"/api/projects/{pid}/full-texts/{trial['item_ref']}",
            params={"actor_id": lead},
            content=LESOTHO.read_bytes(),
            **pdf,
        ),
        201,
    )
    other = ok(
        client.post(
            f"/api/projects/{pid}/full-text-sentinels",
            params={"actor_id": lead, "title": "Vitamin C", "expected_outcome": "exclude"},
            content=VITAMIN_C.read_bytes(),
            **pdf,
        ),
        201,
    )

    runs = f"/api/projects/{pid}/extraction/runs"
    run_body = {
        "command_id": str(new_id()),
        "actor_id": lead,
        "layer_id": text["layer_id"],
        "study_id": trial["study_id"],
        "report_id": trial["report_id"],
        "extraction_schema": schema("lesotho").model_dump(mode="json"),
        "report": {"study_label": "NCT05596773", "title": trial["title"]},
    }

    def request(**changes: Any) -> Any:
        return client.post(runs, json=run_body | {"command_id": str(new_id())} | changes)

    # Included at title and abstract only: no run yet, for the lead either.
    refused = request()
    assert refused.status_code == 409 and "not included at full text" in refused.json()["detail"]

    ft = {"scope_id": SCOPE, "stage": "full_text", "actor_id": lead}
    ok(client.post(f"/api/projects/{pid}/screening/stages", json=ft), 201)
    work()
    still = request()
    assert still.status_code == 409 and "not included at full text" in still.json()["detail"]
    for person in ("Sam", "Jun"):
        # The trial and the full-text sentinel; both included, the sentinel wrongly.
        assert screen(person, "full_text", include_ft) == 2

    # Included now, but a reviewer may not ask, nor may the lead use another document's layer.
    assert request(actor_id=people["Sam"]).status_code == 403
    wrong = request(layer_id=other["layer_id"])
    assert (
        wrong.status_code == 409
        and "not a layer of the report's full text" in wrong.json()["detail"]
    )
    run = ok(request(), 202)
    work()

    def as_(person: str) -> dict[str, str]:
        return {"X-Kittum-Reviewer": people.get(person, stranger)}

    base = f"/api/projects/{pid}/extraction"
    fields = ok(client.get(f"{runs}/{run['id']}/fields", headers=as_("Morgan")))
    participants = next(f for f in fields if f["key"]["endpoint"] == "participants")
    accept = {"command_id": str(new_id()), "path": "janus", "source_viewed_ms": 900}

    # The auditor's audit sample at full text is the trial, and she has not screened it: she
    # learns nothing of the run, and cannot accept its values. Nor can someone outside the review.
    for person in ("Rita", "stranger"):
        assert ok(client.get(runs, headers=as_(person))) == [], person
        for path in ("", "/fields", "/review"):
            hidden = client.get(f"{runs}/{run['id']}{path}", headers=as_(person))
            assert hidden.status_code == 404, (person, path)
        reviewer = people.get(person, stranger)
        refused = client.post(
            f"{base}/fields/{participants['id']}/accept", json=accept | {"reviewer_id": reviewer}
        )
        assert refused.status_code == 404, person
        group = {
            "command_id": str(new_id()),
            "reviewer_id": reviewer,
            "field_ids": [participants["id"]],
            "list_viewed_ms": 900,
        }
        assert client.post(f"{runs}/{run['id']}/group-accept", json=group).status_code == 404

    # The screeners decided it, the third reviewer screens nothing, the lead sees everything.
    for person in ("Morgan", "Sam", "Jun", "Ola"):
        (listed,) = ok(client.get(runs, headers=as_(person)))
        assert listed["run"]["id"] == run["id"], person
        assert ok(client.get(f"{runs}/{run['id']}/review", headers=as_(person)))["fields"]
    accepted = ok(
        client.post(
            f"{base}/fields/{participants['id']}/accept",
            json=accept | {"reviewer_id": people["Ola"]},
        )
    )
    assert accepted["value"] == "52"
    assert [a["value"] for a in ok(client.get(f"{base}/assertions", headers=as_("Sam")))] == ["52"]
    assert ok(client.get(f"{base}/assertions", headers=as_("Rita"))) == []
    assert ok(client.get(f"{base}/assertions", headers=as_("stranger"))) == []

    # Once the auditor has screened her sample, the run is hers to see too.
    assert screen("Rita", "title_abstract", include_ta) >= 1
    assert screen("Rita", "full_text", include_ft) >= 1
    (listed,) = ok(client.get(runs, headers=as_("Rita")))
    assert listed["accepted"] == 1
    assert [a["value"] for a in ok(client.get(f"{base}/assertions", headers=as_("Rita")))] == ["52"]
