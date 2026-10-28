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

import httpx2
import pytest
import yaml
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app import Assistant, recorded_backend
from kittum.assist import ASSIST_EVENT, StrategyAnswer, strict_schema, value_at
from kittum.assist.paths import written
from kittum.cli import main
from kittum.gateway import RecordedTransport, SourceGateway, default_policies
from kittum.ledger import PostgresLedger
from kittum.models import ModelRecipe, OpenAICompatibleBackend
from kittum.protocol import PUBLISHED_EVENT, ReviewProtocol, compile
from kittum.report import load_review, render_files
from kittum.settings import Settings
from kittum.storage import FilesystemStore, MemoryStore
from tests.assist.support import (
    CAPIVASERTIB,
    CONTACT_EMAIL,
    COUNTS,
    DESCRIPTION,
    EXAMPLE,
    PUBMED,
    QUESTION,
    accepted_subset,
)
from tests.documents.conftest import savepoint_db

if TYPE_CHECKING:
    from kittum.db import Database, Tx

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

    def mark(revision: dict[str, Any], path: str) -> dict[str, Any] | None:
        return next((m for m in revision["ai_parts"] if m["path"] == path), None)

    def save(source: dict[str, Any], base: dict[str, Any] | None = None) -> Any:
        body = {"source": source, "base_id": base["id"] if base else None}
        return ok(client.post(f"{url}/revisions", json=body, headers=lead), 201)

    # Attribution follows the value, whichever way the lead saves it. Their own title takes the
    # field over; putting the model's title back, from that revision, gives it back to the model.
    title = draft["document"]["title"]
    own = save({**draft["document"], "title": {"en": "NRT for quitting smoking"}}, draft)
    assert mark(own, "title.en") is None and mark(own, "questions[q1].population.en")
    reverted = save({**own["document"], "title": title}, own)
    assert mark(reverted, "title.en") == {**marks["title.en"], "accepted_in": reverted["id"]}
    # The model's text saved as the lead's own, without a base, is still the model's.
    copied = save(draft["document"])
    assert {m["path"] for m in copied["ai_parts"]} == set(marks)

    # A model that never fixes its answer: three attempts, the problems and the raw answer come
    # back, and no revision is stored; the ledger keeps the attempts.
    revisions_before = len(ok(client.get(f"{url}/revisions", headers=lead)))
    ctx.assistant = Assistant(
        recipe_id=recipe_id,
        backend=Stubborn(backend, artifacts.get(attempts[0]["raw_response_hash"]).decode()),
    )
    failed = ok(client.post(f"{url}/draft", json={"description": DESCRIPTION}, headers=lead))
    assert (failed["revision"], failed["attempts"]) == (None, 3)
    assert failed["issues"][0]["message"] == "2 opening parentheses are not closed"
    assert failed["raw_answer"].startswith("{")

    # A model server that cannot be reached, and one that fails: the attempt is recorded with the
    # bytes that exist, the request and, where there was one, the answer.
    recipe = ModelRecipe(
        id=recipe_id, family="glm", repository="example/glm", revision="1",
        weights_licence="mit", licence_url="https://example.org", serving="hosted",
        context_tokens=1000,
    )  # fmt: skip

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    def overloaded(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"error": "overloaded"}, request=request)

    for server, answered in ((unreachable, False), (overloaded, True)):
        ctx.assistant = Assistant(
            recipe_id=recipe_id,
            backend=OpenAICompatibleBackend(
                recipe, "http://model.test/v1", artifacts, wait_initial=0,
                client=httpx2.Client(transport=httpx2.MockTransport(server)),
            ),
        )  # fmt: skip
        down = ok(client.post(f"{url}/draft", json={"description": DESCRIPTION}, headers=lead))
        assert (down["revision"], down["attempts"]) == (None, 1)
        assert down["issues"][0]["message"].startswith("the model server failed: glm-5.3-flash: ")
        (attempt,) = events(ASSIST_EVENT)[-1]["attempts"]
        assert json.loads(artifacts.get(attempt["raw_request_hash"]))["model"] == "example/glm"
        if answered:
            assert b"overloaded" in artifacts.get(attempt["raw_response_hash"])
        else:
            assert attempt["raw_response_hash"] is None
    assert len(ok(client.get(f"{url}/revisions", headers=lead))) == revisions_before
    assert [(e["outcome"], len(e["attempts"])) for e in events(ASSIST_EVENT)] == [
        ("revision", 3),
        ("failed", 3),
        ("failed", 1),
        ("failed", 1),
    ]
    ctx.assistant = Assistant(recipe_id=recipe_id, backend=backend)

    # The lead brings in the example protocol as their own text. Only what says word for word
    # what the model wrote at the same place is the model's.
    example = yaml.safe_load(EXAMPLE.read_text())
    mine = save(example)
    assert (mine["kind"], mine["parent_id"]) == ("edit", copied["id"])
    drafted_values = written(draft["document"])
    shared = {p for p, v in written(mine["document"]).items() if drafted_values.get(p) == v}
    assert {m["path"] for m in mine["ai_parts"]} == shared
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
    assert mark(suggestion, PUBMED) == {
        **marks[medline], "path": PUBMED, "revision_id": suggestion["id"], "accepted_in": None,
    }  # fmt: skip
    sent = json.loads(artifacts.get(suggestion["model"]["attempts"][0]["raw_request_hash"]))
    assert sent["json_schema"] == strict_schema(StrategyAnswer)

    # Count before, with every change, and with the two the lead takes; and the registry.
    chosen = accepted_subset([c["id"] for c in changes])
    expected = before
    for change in sorted((c for c in changes if c["id"] in chosen), key=lambda c: -c["start"]):
        expected = (
            expected[: change["start"]]
            + change["after"]
            + expected[change["start"] + len(change["before"]) :]
        )

    def count(strategy: str, who: dict[str, str] = lead, dialect: str = "pubmed") -> Any:
        return client.post(
            f"{url}/count", json={"dialect": dialect, "strategy": strategy}, headers=who
        )

    def strategy_of(revision: dict[str, Any], source: int = 1) -> str:
        return revision["document"]["search_topics"][0]["source_plan"]["sources"][source][
            "strategy"
        ]

    assert count(before, sam).status_code == 403
    counted = [ok(count(s)) for s in (before, strategy_of(suggestion), expected)]
    assert [c["count"] for c in counted] == [940, 2311, 1363]
    assert counted[0]["query_translation"] and all(artifacts.exists(c["raw_hash"]) for c in counted)
    registry = ok(count(strategy_of(mine, 4), dialect="ctgov"))
    assert (registry["count"], registry["query_translation"]) == (673, None)

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
    assert (mark(accepted, PUBMED) or {})["revision_id"] == suggestion["id"]
    assert (mark(accepted, PUBMED) or {})["accepted_in"] == accepted["id"]
    assert {m["path"] for m in accepted["ai_parts"]} == shared | {PUBMED}
    difference = ok(client.get(f"{url}/revisions/{mine['id']}/diff/{accepted['id']}", headers=lead))
    assert [c["path"] for c in difference["changes"]] == [PUBMED]

    # Step back: restore the lead's own version, then the accepted one again.
    back = ok(client.post(f"{url}/revisions/{mine['id']}/restore", headers=lead), 201)
    assert (back["kind"], back["document"]) == ("restore", mine["document"])
    assert {m["path"] for m in back["ai_parts"]} == shared
    again = ok(client.post(f"{url}/revisions/{accepted['id']}/restore", headers=lead), 201)
    assert again["document"] == accepted["document"] and again["ai_parts"] == accepted["ai_parts"]
    seqs = [r["seq"] for r in ok(client.get(f"{url}/revisions", headers=lead))]
    assert seqs == list(range(1, 10))

    # Publish: the lead's approval, with the model's fields in the ledger event.
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
    assert {p["path"] for p in published["ai_parts"]} == shared | {PUBMED}
    strategy_part = next(p for p in published["ai_parts"] if p["path"] == PUBMED)
    assert (strategy_part["revision_id"], strategy_part["accepted_in"]) == (
        suggestion["id"],
        accepted["id"],
    )

    # The plain editor: a next version keeps the mark on the strategy it leaves alone, and the
    # model's own draft, edited and put back word for word, is the model's wherever it is saved.
    retitled = {**again["document"], "title": {"en": "NRT for smoking cessation"}}

    def publish_plainly(source: dict[str, Any], reason: str) -> dict[str, Any]:
        version = ok(client.post(f"/api/projects/{pid}/protocol-versions", json=source), 201)
        approval = {"approved_by": lead["X-Kittum-Reviewer"], "reason": reason}
        ok(
            client.post(
                f"/api/projects/{pid}/protocol-versions/{version['id']}/publish", json=approval
            )
        )
        return events(PUBLISHED_EVENT)[-1]

    assert {p["path"] for p in publish_plainly(retitled, "Shorter title")["ai_parts"]} == (
        shared - {"title.en"}
    ) | {PUBMED}
    plainly = publish_plainly(reverted["document"], "The drafted protocol")
    assert {p["path"] for p in plainly["ai_parts"]} == set(marks)

    # A question improved in a profile's protocol: the model rewrites the comparators, and the
    # profile's extensions on them (off-label use) survive, though the model never saw them.
    capivasertib = save(
        json.loads(json.dumps(yaml.safe_load(CAPIVASERTIB.read_text()), default=str))
    )
    off_label = {
        c["id"]
        for c in value_at(capivasertib["document"], QUESTION)["comparators"]
        if c["extensions"]["off_label"]
    }
    assert off_label == {"letrozol", "exemestan", "fulvestrant"}
    question = ok(
        client.post(
            f"{url}/revisions/{capivasertib['id']}/improve", json={"part": QUESTION}, headers=lead
        )
    )["revision"]
    (rewritten,) = question["suggestion"]["changes"]
    assert rewritten["field"] == "comparators" and rewritten["before"] != rewritten["after"]
    taken = ok(
        client.post(
            f"{url}/revisions/{question['id']}/accept", json={"change_ids": ["c1"]}, headers=lead
        ),
        201,
    )
    comparators = value_at(taken["document"], QUESTION)["comparators"]
    assert all(c["text"].get("en") for c in comparators)  # the model's change is in
    assert {c["id"] for c in comparators if c["extensions"]["off_label"]} == off_label

    # The AI disclosure names the fields, the model, and every request with its attempts.
    with db.transaction() as tx:
        review = load_review(tx, pid)
    files = {f.path: f.data for f in render_files(review, "ai_disclosure")}
    disclosed = json.loads(files["ai_disclosure.json"])["protocol_assistant"]
    assert {p["path"] for p in disclosed["ai_parts"]} == set(marks)
    assert [
        (r["purpose"], r["outcome"], r["attempts"], r["rejected_attempts"])
        for r in disclosed["requests"]
    ] == [
        ("draft", "revision", 3, 2),
        ("draft", "failed", 3, 3),
        ("draft", "failed", 1, 1),
        ("draft", "failed", 1, 1),
        ("improve", "revision", 1, 0),
        ("improve", "revision", 1, 0),
    ]
    text = files["ai_disclosure.md"].decode()
    assert "`title.en`" in text and "recorded:glm-5.3-flash" in text


def test_a_drafted_protocol_published_from_the_command_line_names_what_the_model_wrote(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("KITTUM_ARTIFACT_DIR", str(tmp_path))
    artifacts = FilesystemStore(tmp_path)
    recipe_id, backend = recorded_backend(artifacts)

    def kittum(*args: str) -> Any:
        assert main(list(args)) == 0
        return json.loads(capsys.readouterr().out)

    created = kittum("project", "create", "NRT from the shell", "--lead", "Morgan Lee")
    pid, lead = created["project_id"], created["lead"]
    ctx = AppContext(
        settings=Settings(database_url="postgresql://unused", artifact_dir=tmp_path),
        db=database,
        artifacts=artifacts,
        ledger=PostgresLedger(),
        assistant=Assistant(recipe_id=recipe_id, backend=backend),
    )
    client = TestClient(create_app(ctx))
    drafted = client.post(
        f"/api/projects/{pid}/assist/draft",
        json={"description": DESCRIPTION},
        headers={"X-Kittum-Reviewer": lead},
    ).json()["revision"]

    # The lead takes the draft out as a file and publishes it with the command line.
    exported = tmp_path / "protocol.yaml"
    exported.write_text(yaml.safe_dump(drafted["document"], sort_keys=False, allow_unicode=True))
    kittum("project", "publish-protocol", pid, str(exported), "--by", lead)

    with database.transaction() as tx:
        (published,) = [e for e in PostgresLedger().events(tx, pid) if e.type == PUBLISHED_EVENT]
    assert {p["path"] for p in published.payload["ai_parts"]} == {
        m["path"] for m in drafted["ai_parts"]
    }
    assert {p["revision_id"] for p in published.payload["ai_parts"]} == {drafted["id"]}
