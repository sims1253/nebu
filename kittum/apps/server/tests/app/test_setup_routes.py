"""A review set up over HTTP the way the workbench does it, from the files a lead has on disk.

The workbench cannot read YAML itself and has no protocol of its own, so the server reads each
file it is given: the protocol (a syntax error comes back with its line), the sentinels, and the
searches (a PubMed XML export among them). Before publishing, the lead sees what the new version
changes. After the corpus is built, the lead's upload queue lists every report with its outcomes
and full text; reviewers get none of it, and only the lead may add full-text sentinels, which never
show in that queue. Runs on a rolled-back connection, so no document outlives the test.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app.demo import DATA, PDFS, PROTOCOL, demo_judge
from kittum.ledger import PostgresLedger
from kittum.models import ModelRegistry
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.documents.conftest import savepoint_db

if TYPE_CHECKING:
    from kittum.db import Tx

pytestmark = pytest.mark.postgres

SENTINELS = PROTOCOL.parents[1] / "sentinels" / "nicotine-replacement.yaml"
LESOTHO = PDFS / "gerber2026-lesotho-t2d-crct.pdf"
VITAMIN_C = PDFS / "li2026-vitamin-c-kidney-biopsy-rct.pdf"


def test_a_lead_sets_up_a_review_from_files_over_http(pg: Tx) -> None:
    ctx = AppContext(
        settings=Settings(database_url="postgresql://unused", artifact_dir=Path("unused")),
        db=savepoint_db(pg),
        artifacts=MemoryStore(),
        ledger=PostgresLedger(),
        judge=demo_judge(ModelRegistry.load()),
    )
    client = TestClient(create_app(ctx))

    def ok(response: Any, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    project = ok(client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan Lee"}), 201)
    pid = project["id"]
    lead = project["members"][0]["actor_id"]
    as_lead = {"X-Kittum-Reviewer": lead}
    people = {}
    roles = {"Sam": "reviewer", "Jun": "reviewer", "Rita": "auditor", "Elena": "adjudicator"}
    for name, role in roles.items():
        body = {"display_name": name, "roles": [role], "added_by": lead}
        people[name] = ok(client.post(f"/api/projects/{pid}/members", json=body), 201)["actor_id"]
    as_sam = {"X-Kittum-Reviewer": people["Sam"]}

    # The lead starts from the example protocol the checkout ships.
    examples = {e["id"]: e for e in ok(client.get("/api/protocol/examples"))}
    example = examples["nicotine-replacement"]
    assert example["title"] == "Nicotine replacement therapy for smoking cessation"
    assert example["text"] == PROTOCOL.read_text()

    # A slip in the YAML names its line; a protocol that parses but does not compile names the
    # field at fault, and nothing is saved.
    broken = example["text"].replace("version_label: ", "version_label: [", 1)
    (issue,) = ok(client.post("/api/protocol/parse", json={"text": broken}))["issues"]
    assert issue["message"].startswith("the protocol is not valid YAML or JSON: line 1")
    source = ok(client.post("/api/protocol/parse", json={"text": example["text"]}))["source"]
    incomplete = {k: v for k, v in source.items() if k != "search_topics"}
    report = ok(client.post("/api/protocol/validate", json=incomplete))
    assert not report["ok"] and "search_topics" in {i["path"] for i in report["issues"]}
    refused = client.post(f"/api/projects/{pid}/protocol/diff", json=incomplete)
    assert refused.status_code == 422
    assert ok(client.get(f"/api/projects/{pid}/protocol-versions")) == []

    # The first version has nothing before it.
    preview = ok(client.post(f"/api/projects/{pid}/protocol/diff", json=source))
    assert (preview["previous"], preview["diff"]) == (None, None)
    draft = ok(client.post(f"/api/projects/{pid}/protocol-versions", json=source), 201)
    assert draft["version"]["content_hash"] == preview["content_hash"]
    ok(
        client.post(
            f"/api/projects/{pid}/protocol-versions/{draft['id']}/publish",
            json={"approved_by": lead, "reason": "First version"},
        )
    )

    # The next version starts from the published one, which reads back to the same hash, and
    # the preview names what the edit changes.
    text = ok(client.get(f"/api/projects/{pid}/protocol/source"))["text"]
    again = ok(client.post("/api/protocol/parse", json={"text": text}))["source"]
    assert (
        ok(client.post("/api/protocol/validate", json=again))["content_hash"]
        == (draft["version"]["content_hash"])
    )
    edited = ok(
        client.post(
            "/api/protocol/parse",
            json={"text": text.replace("Adults (18 years or older) who smoke", "Adults who smoke")},
        )
    )["source"]
    preview = ok(client.post(f"/api/projects/{pid}/protocol/diff", json=edited))
    assert preview["previous"]["id"] == draft["id"]
    assert preview["diff"]["changed_criteria"] == ["POP"]
    assert [c["path"] for c in preview["diff"]["changes"]] == [
        "search_topics[TRIALS].criteria[POP].text.en"
    ]
    assert len(ok(client.get(f"/api/projects/{pid}/protocol-versions"))) == 1  # nothing saved

    # Searches from export files: PubMed XML and two RIS files, the demo's three sources.
    for source_id, platform, file, format in (
        ("pubmed", "PubMed", "pubmed_efetch.xml", "pubmed-xml"),
        ("central", "Cochrane Library (Wiley)", "central_export.ris", "ris"),
        ("citation_search", "Reference lists", "citation_search.ris", "ris"),
    ):
        plan = {"topic_id": "TRIALS", "source": source_id, "platform": platform, "strategy": "x"}
        body = {"plan": plan, "data": (DATA / file).read_text(), "format": format}
        execution = ok(
            client.post(f"/api/projects/{pid}/search/import", json=body | {"actor_id": lead})
        )
        assert {r["source"] for r in execution["records"]} == {source_id}
    assert execution["retrieved_count"] == 3
    garbled = {"plan": plan, "data": "<PubmedArticleSet>", "format": "pubmed-xml"}
    assert (
        client.post(
            f"/api/projects/{pid}/search/import", json=garbled | {"actor_id": lead}
        ).status_code
        == 400
    )

    # No upload queue before the corpus; then every report, and none for a reviewer.
    assert client.get(f"/api/projects/{pid}/full-texts", headers=as_lead).status_code == 409
    ok(client.post(f"/api/projects/{pid}/corpus/ingest", json={"actor_id": lead}), 201)
    queue = ok(client.get(f"/api/projects/{pid}/full-texts", headers=as_lead))
    assert len(queue) == 30  # the duplicate on a DOI is proposed, not merged, until confirmed
    assert all(item["full_text"] is None and item["outcomes"] == {} for item in queue)
    assert client.get(f"/api/projects/{pid}/full-texts", headers=as_sam).status_code == 403
    trial = next(i for i in queue if i["doi"] == "10.1186/s12916-026-04943-4")
    attached = ok(
        client.post(
            f"/api/projects/{pid}/full-texts/{trial['item_ref']}",
            params={"actor_id": lead, "licence": "CC BY 4.0"},
            content=LESOTHO.read_bytes(),
            headers={"Content-Type": "application/pdf"},
        ),
        201,
    )
    assert attached["page_count"] == 13

    # Only the lead adds a full-text sentinel, and it never joins the queue of reports.
    sentinel = {"title": "Vitamin C after kidney biopsy", "expected_outcome": "exclude"}
    pdf = {"content": VITAMIN_C.read_bytes(), "headers": {"Content-Type": "application/pdf"}}
    path = f"/api/projects/{pid}/full-text-sentinels"
    by_sam = client.post(path, params=sentinel | {"actor_id": people["Sam"]}, **pdf)
    assert by_sam.status_code == 403
    ok(client.post(path, params=sentinel | {"actor_id": lead}, **pdf), 201)
    not_pdf = client.post(path, params=sentinel | {"actor_id": lead}, content=b"%PDX nothing")
    assert not_pdf.status_code == 415

    # The sentinel file as `--sentinels` reads it; a wrong entry is named.
    bad = "- expected_outcome: maybe\n  record: {item_ref: s, title: t, text: x}\n"
    refused = client.post(f"/api/projects/{pid}/screening/sentinels/parse", json={"text": bad})
    assert refused.status_code == 422
    assert "sentinel 1 in the sentinel file: expected_outcome" in refused.json()["detail"][0]["msg"]
    sentinels = ok(
        client.post(
            f"/api/projects/{pid}/screening/sentinels/parse", json={"text": SENTINELS.read_text()}
        )
    )
    assert [s["record"]["item_ref"] for s in sentinels] == [
        "sentinel:nrt-lozenge",
        "sentinel:nrt-spray",
    ]
    stage = {"scope_id": "nrt-vs-control", "stage": "title_abstract", "actor_id": lead}
    ok(
        client.post(f"/api/projects/{pid}/screening/stages", json=stage | {"sentinels": sentinels}),
        201,
    )

    # The queue now shows each report's outcome in the open stage (none yet), its full text, and
    # still no sentinel of either kind.
    queue = ok(client.get(f"/api/projects/{pid}/full-texts", headers=as_lead))
    assert len(queue) == 30
    assert all(i["outcomes"] == {"nrt-vs-control": {"title_abstract": None}} for i in queue)
    assert [i["full_text"]["layer_id"] for i in queue if i["full_text"]] == [attached["layer_id"]]
    assert not any(i["item_ref"].startswith("sentinel:") for i in queue)
