"""A lead writes the protocol with the assistant over HTTP, on GLM-5.3-Flash's recorded answers
and PubMed's recorded counts.

The draft follows a JSON Schema derived from the protocol's own models, and each answer goes
through the protocol compiler: the model's first two drafts left a parenthesis open in the PubMed
strategy, so Kittum sent the problems back and the third draft became the revision. A model that
never fixes its answer gets nothing stored but the record of its attempts. Then the lead loads
the example protocol, asks for a better PubMed strategy, counts it before and after, accepts two
of the three changes, steps back and restores, and publishes: the ledger event and the AI
disclosure name the field the model wrote. A reviewer may do none of it.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app import Assistant, recorded_backend
from kittum.assist import ASSIST_EVENT, StrategyAnswer, strict_schema
from kittum.gateway import RecordedTransport, SourceGateway, default_policies
from kittum.ledger import PostgresLedger
from kittum.protocol import PUBLISHED_EVENT, ReviewProtocol, compile
from kittum.report import load_review, render_files
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.assist.support import (
    CONTACT_EMAIL,
    COUNTS,
    DESCRIPTION,
    EXAMPLE,
    PUBMED,
    accepted_subset,
)
from tests.documents.conftest import savepoint_db

if TYPE_CHECKING:
    from kittum.db import Tx

pytestmark = pytest.mark.postgres


def _objects(schema: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            found.append(schema)
        for value in schema.values():
            found += _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            found += _objects(value)
    return found


class Stubborn:
    """A model that answers every request with GLM's first, uncompilable draft."""

    def __init__(self, inner: Any, first: str) -> None:
        self._inner = inner
        self._first = first

    def complete(self, request: Any) -> Any:
        first = request.model_copy(update={"messages": request.messages[:2]})
        return self._inner.complete(first).model_copy(update={"text": self._first})


def test_a_lead_drafts_improves_and_publishes_a_protocol_with_the_assistant(pg: Tx) -> None:
    artifacts = MemoryStore()
    db = savepoint_db(pg)
    recipe_id, backend = recorded_backend(artifacts)
    ctx = AppContext(
        settings=Settings(
            database_url="postgresql://unused",
            artifact_dir=Path("unused"),
            contact_email=CONTACT_EMAIL,
        ),
        db=db,
        artifacts=artifacts,
        ledger=PostgresLedger(),
        gateway=SourceGateway(
            database=db,
            transport=RecordedTransport(COUNTS),
            artifacts=artifacts,
            policies=default_policies(contact_email=CONTACT_EMAIL),
            buckets="memory",
            cache="memory",
        ),
        assistant=Assistant(recipe_id=recipe_id, backend=backend),
    )
    client = TestClient(create_app(ctx))

    def ok(response: Any, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    project = ok(client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan Lee"}), 201)
    pid = project["id"]
    lead = {"X-Kittum-Reviewer": project["members"][0]["actor_id"]}
    body = {"display_name": "Sam", "roles": ["reviewer"], "added_by": lead["X-Kittum-Reviewer"]}
    sam = {
        "X-Kittum-Reviewer": ok(client.post(f"/api/projects/{pid}/members", json=body), 201)[
            "actor_id"
        ]
    }
    url = f"/api/projects/{pid}/assist"

    def events(kind: str) -> list[dict[str, Any]]:
        with db.transaction() as tx:
            return [e.payload for e in PostgresLedger().events(tx, pid) if e.type == kind]

    # A reviewer may not ask; nothing reaches the model or the ledger.
    refused = client.post(f"{url}/draft", json={"description": DESCRIPTION}, headers=sam)
    assert refused.status_code == 403
    assert client.get(f"{url}/revisions", headers=sam).status_code == 403

    # The draft: two answers left the PubMed strategy's parentheses open; the third compiled.
    outcome = ok(client.post(f"{url}/draft", json={"description": DESCRIPTION}, headers=lead))
    draft = outcome["revision"]
    assert (draft["kind"], draft["seq"], outcome["attempts"]) == ("draft", 1, 3)
    attempts = draft["model"]["attempts"]
    assert [[(i["path"], i["message"]) for i in a["issues"]] for a in attempts] == [
        [
            (
                "search_topics[0].source_plan.sources[0].strategy",
                "2 opening parentheses are not closed",
            )
        ],
        [
            (
                "search_topics[0].source_plan.sources[0].strategy",
                "a closing parenthesis has no opening one",
            )
        ],
        [],
    ]
    version = compile(draft["document"])
    assert draft["content_hash"] == version.content_hash
    assert draft["model"]["recipe_id"] == "glm-5.3-flash"
    # The repair round sent the problems back with the model's own answer.
    second = json.loads(artifacts.get(attempts[1]["raw_request_hash"]))
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "user"]
    assert "2 opening parentheses are not closed" in second["messages"][3]["content"]
    assert (
        artifacts.get(attempts[0]["raw_response_hash"]).decode() == second["messages"][2]["content"]
    )

    # The schema the model answered in is the one derived from the protocol model, in strict form.
    sent = json.loads(artifacts.get(attempts[0]["raw_request_hash"]))["json_schema"]
    assert sent == strict_schema(ReviewProtocol)
    for node in _objects(sent):
        if "properties" in node:
            assert sorted(node["required"]) == sorted(node["properties"])
            assert node["additionalProperties"] is False
    assert '"default"' not in json.dumps(sent)

    class Annotated(ReviewProtocol):
        reviewer_note: str | None = None

    changed = strict_schema(Annotated)
    assert changed != sent and "reviewer_note" in changed["required"]

    # The model wrote every field of the draft.
    marks = {m["path"]: m for m in draft["ai_parts"]}
    medline = "search_topics[topic_nrt].source_plan.sources[medline].strategy"
    assert {"title.en", "questions[q1].population.en", medline} <= set(marks)
    assert {(m["revision_id"], m["recipe_id"], m["accepted_in"]) for m in marks.values()} == {
        (draft["id"], "glm-5.3-flash", None)
    }
    (drafted,) = events(ASSIST_EVENT)
    assert (drafted["outcome"], drafted["revision_id"], len(drafted["attempts"])) == (
        "revision",
        draft["id"],
        3,
    )

    # A model that never fixes its answer: three attempts, the problems and the raw answer come
    # back, and no revision is stored; the ledger keeps the attempts.
    ctx.assistant = Assistant(
        recipe_id=recipe_id,
        backend=Stubborn(backend, artifacts.get(attempts[0]["raw_response_hash"]).decode()),
    )
    failed = ok(client.post(f"{url}/draft", json={"description": DESCRIPTION}, headers=lead))
    assert (failed["revision"], failed["attempts"]) == (None, 3)
    assert failed["issues"][0]["message"] == "2 opening parentheses are not closed"
    assert failed["raw_answer"].startswith("{")
    assert len(ok(client.get(f"{url}/revisions", headers=lead))) == 1
    assert [(e["outcome"], len(e["attempts"])) for e in events(ASSIST_EVENT)] == [
        ("revision", 3),
        ("failed", 3),
    ]
    ctx.assistant = Assistant(recipe_id=recipe_id, backend=backend)

    # The lead brings in the example protocol as their own revision: nothing in it is the model's.
    example = yaml.safe_load(EXAMPLE.read_text())
    mine = ok(client.post(f"{url}/revisions", json={"source": example}, headers=lead), 201)
    assert (mine["kind"], mine["parent_id"], mine["ai_parts"]) == ("edit", draft["id"], [])
    broken = {**example, "questions": []}
    refused = client.post(f"{url}/revisions", json={"source": broken}, headers=lead)
    assert (
        refused.status_code == 422 and refused.json()["detail"]["issues"][0]["path"] == "questions"
    )

    # Improve its PubMed strategy: three changes, each with a reason, as a suggestion revision.
    before = example["search_topics"][0]["source_plan"]["sources"][1]["strategy"]
    assert (
        client.post(
            f"{url}/revisions/{mine['id']}/improve", json={"part": PUBMED}, headers=sam
        ).status_code
        == 403
    )
    improved = ok(
        client.post(f"{url}/revisions/{mine['id']}/improve", json={"part": PUBMED}, headers=lead)
    )
    suggestion = improved["revision"]
    assert (suggestion["kind"], suggestion["parent_id"], improved["attempts"]) == (
        "suggestion",
        mine["id"],
        1,
    )
    changes = suggestion["suggestion"]["changes"]
    assert [c["id"] for c in changes] == ["c1", "c2", "c3"]
    assert all(c["rationale"] and before[c["start"] :].startswith(c["before"]) for c in changes)
    assert "tobacco use cessation" in changes[1]["after"]
    assert [(m["path"], m["revision_id"], m["accepted_in"]) for m in suggestion["ai_parts"]] == [
        (PUBMED, suggestion["id"], None)
    ]
    sent = json.loads(artifacts.get(suggestion["model"]["attempts"][0]["raw_request_hash"]))
    assert sent["json_schema"] == strict_schema(StrategyAnswer)

    # Count before, with every change, and with the two the lead takes.
    chosen = accepted_subset([c["id"] for c in changes])
    expected = before
    for change in sorted((c for c in changes if c["id"] in chosen), key=lambda c: -c["start"]):
        expected = (
            expected[: change["start"]]
            + change["after"]
            + expected[change["start"] + len(change["before"]) :]
        )

    def count(strategy: str, who: dict[str, str] = lead) -> Any:
        return client.post(
            f"{url}/count", json={"dialect": "pubmed", "strategy": strategy}, headers=who
        )

    def strategy_of(revision: dict[str, Any]) -> str:
        return revision["document"]["search_topics"][0]["source_plan"]["sources"][1]["strategy"]

    assert count(before, sam).status_code == 403
    counted = [ok(count(s)) for s in (before, strategy_of(suggestion), expected)]
    assert [c["count"] for c in counted] == [940, 2311, 1363]
    assert counted[0]["query_translation"] and all(artifacts.exists(c["raw_hash"]) for c in counted)

    # Accept two of three: the base with exactly those, the field marked as the model's.
    assert (
        client.post(
            f"{url}/revisions/{suggestion['id']}/accept", json={"change_ids": ["c9"]}, headers=lead
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{url}/revisions/{suggestion['id']}/accept", json={"change_ids": chosen}, headers=sam
        ).status_code
        == 403
    )
    accepted = ok(
        client.post(
            f"{url}/revisions/{suggestion['id']}/accept", json={"change_ids": chosen}, headers=lead
        ),
        201,
    )
    assert (accepted["kind"], accepted["parent_id"], accepted["accepted"]["change_ids"]) == (
        "accepted",
        mine["id"],
        ["c1", "c2"],
    )
    assert strategy_of(accepted) == expected
    assert [(m["path"], m["revision_id"], m["accepted_in"]) for m in accepted["ai_parts"]] == [
        (PUBMED, suggestion["id"], accepted["id"])
    ]
    difference = ok(client.get(f"{url}/revisions/{mine['id']}/diff/{accepted['id']}", headers=lead))
    assert [c["path"] for c in difference["changes"]] == [PUBMED]

    # Step back: restore the lead's own version, then the accepted one again.
    back = ok(client.post(f"{url}/revisions/{mine['id']}/restore", headers=lead), 201)
    assert (back["kind"], back["document"], back["ai_parts"]) == ("restore", mine["document"], [])
    again = ok(client.post(f"{url}/revisions/{accepted['id']}/restore", headers=lead), 201)
    assert again["document"] == accepted["document"] and again["ai_parts"] == accepted["ai_parts"]
    assert [r["seq"] for r in ok(client.get(f"{url}/revisions", headers=lead))] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]

    # Publish: the lead's approval, with the model's field in the ledger event.
    assert (
        client.post(f"{url}/revisions/{again['id']}/publish", json={}, headers=sam).status_code
        == 403
    )
    stored = ok(
        client.post(
            f"{url}/revisions/{again['id']}/publish", json={"reason": "First version"}, headers=lead
        )
    )
    assert stored["status"] == "published"
    (published,) = events(PUBLISHED_EVENT)
    assert published["content_hash"] == again["content_hash"]
    assert [
        (p["path"], p["revision_id"], p["accepted_in"], p["recipe_id"])
        for p in published["ai_parts"]
    ] == [(PUBMED, suggestion["id"], accepted["id"], "glm-5.3-flash")]

    # A next version written in the plain editor keeps the mark on the strategy it leaves alone.
    retitled = {**again["document"], "title": {"en": "NRT for smoking cessation"}}
    version = ok(client.post(f"/api/projects/{pid}/protocol-versions", json=retitled), 201)
    approval = {"approved_by": lead["X-Kittum-Reviewer"], "reason": "Shorter title"}
    ok(client.post(f"/api/projects/{pid}/protocol-versions/{version['id']}/publish", json=approval))
    assert [p["path"] for p in events(PUBLISHED_EVENT)[1]["ai_parts"]] == [PUBMED]

    # The AI disclosure names the field, the model, and every request with its attempts.
    with db.transaction() as tx:
        review = load_review(tx, pid)
    files = {f.path: f.data for f in render_files(review, "ai_disclosure")}
    disclosed = json.loads(files["ai_disclosure.json"])["protocol_assistant"]
    assert [p["path"] for p in disclosed["ai_parts"]] == [PUBMED]
    assert [
        (r["purpose"], r["outcome"], r["attempts"], r["rejected_attempts"])
        for r in disclosed["requests"]
    ] == [
        ("draft", "revision", 3, 2),
        ("draft", "failed", 3, 3),
        ("improve", "revision", 1, 0),
    ]
    text = files["ai_disclosure.md"].decode()
    assert f"`{PUBMED}`" in text and "recorded:glm-5.3-flash" in text
