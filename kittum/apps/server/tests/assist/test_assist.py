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
import os
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
import yaml
from fastapi.testclient import TestClient

from kittum import assist
from kittum.api import AppContext, create_app
from kittum.app import (
    Assistant,
    ConfigurationError,
    build_assistant,
    build_backends,
    build_judge,
    recorded_backend,
)
from kittum.assist import ASSIST_EVENT, StrategyAnswer, strict_schema, value_at
from kittum.assist.paths import written
from kittum.cli import main
from kittum.gateway import RecordedTransport, SourceGateway, default_policies
from kittum.ledger import PostgresLedger
from kittum.models import (
    CompletionRequest,
    Message,
    ModelRecipe,
    ModelRegistry,
    OpenAICompatibleBackend,
    request_key,
)
from kittum.protocol import PUBLISHED_EVENT, ReviewProtocol, compile
from kittum.report import load_review, render_files
from kittum.settings import LlmEndpoint, Settings
from kittum.storage import FilesystemStore, MemoryStore
from tests.assist.support import (
    ANSWERS,
    CAPIVASERTIB,
    CONTACT_EMAIL,
    COUNTS,
    DESCRIPTION,
    EXAMPLE,
    MISTYPED,
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


def renamed_topic(topic: dict[str, Any], name: str) -> dict[str, Any]:
    """`topic` under another ID, its criteria applying to it under that ID."""

    def applies(criterion: dict[str, Any]) -> dict[str, Any]:
        targets = [name if t == topic["id"] else t for t in criterion["applies_to"]]
        return {**criterion, "applies_to": targets}

    return {**topic, "id": name, "criteria": [applies(c) for c in topic["criteria"]]}


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
    # So is a criterion the lead renames and moves to the end, its words unchanged.
    topic = draft["document"]["search_topics"][0]
    first, *rest = topic["criteria"]
    moved = save(
        {
            **draft["document"],
            "search_topics": [{**topic, "criteria": [*rest, {**first, "id": "renumbered"}]}],
        },
        copied,
    )
    renamed = mark(moved, f"search_topics[{topic['id']}].criteria[renumbered].text.en")
    assert renamed and renamed["revision_id"] == draft["id"]
    assert mark(moved, f"search_topics[{topic['id']}].criteria[renumbered].id") is None
    # A renamed topic is still the model's topic: its words say so, and its mode, numbers, and
    # other choices stay the model's, at their new place. What the lead changed is theirs.
    longer = {**topic["freshness"], "max_age_months": 24}
    renamed_one = {**renamed_topic(topic, "renamed"), "freshness": longer}
    retopic = save({**draft["document"], "search_topics": [renamed_one]}, moved)
    for field in (
        "mode",
        "audit.random_audit_percent",
        f"criteria[{first['id']}].polarity",
        f"criteria[{first['id']}].slot",
        "source_plan.sources[medline].platform",
        "label.en",
        "source_plan.sources[medline].strategy",
    ):
        chosen = mark(retopic, f"search_topics[renamed].{field}")
        assert chosen and chosen["revision_id"] == draft["id"], field
    assert mark(retopic, "search_topics[renamed].freshness.max_age_months") is None

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

    # The lead brings in the example protocol as their own text. Only words the model wrote for
    # that kind of field are the model's ("Serious adverse events" as an outcome's name); the
    # example's choices and numbers are its own, though the draft chose the same elsewhere.
    example = yaml.safe_load(EXAMPLE.read_text())
    mine = save(example)
    assert (mine["kind"], mine["parent_id"]) == ("edit", retopic["id"])
    shared = {m["path"] for m in mine["ai_parts"]}
    assert shared == {"outcomes[sae].name.en"}

    def kind(path: str) -> str:
        return re.sub(r"\[[^\]]*\]", "[*]", path)

    drafted_choices = {
        (kind(p), json.dumps(v))
        for p, v in written(draft["document"]).items()
        if p.endswith((".polarity", ".max_age_months", ".platform", ".random_audit_percent"))
    }
    own_choices = {
        p
        for p, v in written(mine["document"]).items()
        if (kind(p), json.dumps(v)) in drafted_choices
    }
    assert "search_topics[TRIALS].criteria[INT].polarity" in own_choices  # chosen alike
    assert "search_topics[TRIALS].freshness.max_age_months" in own_choices
    assert "search_topics[TRIALS].source_plan.sources[medline].platform" in own_choices
    assert not shared & own_choices
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
    # A mistyped strategy still counts, and says what PubMed found nowhere or left out.
    mistyped = ok(count(MISTYPED))
    assert (mistyped["count"], mistyped["warnings"]) == (
        35484,
        [
            "PubMed did not find the phrase: nicotinne",
            "PubMed ignored: by",
            'PubMed did not find the quoted phrase: "nicotine replacment zzqx"[tiab]',
        ],
    )
    # Offline, a strategy nobody recorded is an unreachable source, named by its request.
    unrecorded = count("nicotine lozenge[tiab]")
    assert unrecorded.status_code == 502, unrecorded.text
    assert re.fullmatch(
        r"pubmed is unreachable: no recorded response for https://eutils\.ncbi\.nlm\.nih\.gov/\S+",
        unrecorded.json()["detail"],
    )

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
    assert seqs == list(range(1, 12))

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


PUBLISH_ALONE = """
import json, sys
from kittum.db import Database
from kittum.ledger import PostgresLedger
from kittum.protocol import ProtocolStateError, compile, publish, save_draft
{imports}
url, project, lead, path = sys.argv[1:5]
assert "kittum.app" not in sys.modules
try:
    with Database(url).transaction() as tx:
        draft = save_draft(tx, project, compile(json.loads(open(path).read())))
        publish(tx, project, draft.id, lead, ledger=PostgresLedger())
except ProtocolStateError as e:
    print(e)
    sys.exit(3)
"""


def test_a_publisher_without_the_assistant_refuses_rather_than_hide_what_a_model_wrote(
    database: Database, tmp_path: Path
) -> None:
    artifacts = FilesystemStore(tmp_path)
    recipe_id, backend = recorded_backend(artifacts)
    ctx = AppContext(
        settings=Settings(database_url="postgresql://unused", artifact_dir=tmp_path),
        db=database,
        artifacts=artifacts,
        ledger=PostgresLedger(),
        assistant=Assistant(recipe_id=recipe_id, backend=backend),
    )
    client = TestClient(create_app(ctx))
    project = client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan Lee"}).json()
    pid, lead = project["id"], project["members"][0]["actor_id"]
    drafted = client.post(
        f"/api/projects/{pid}/assist/draft",
        json={"description": DESCRIPTION},
        headers={"X-Kittum-Reviewer": lead},
    ).json()["revision"]
    exported = tmp_path / "protocol.json"
    exported.write_text(json.dumps(drafted["document"]))

    def publish_alone(imports: str) -> subprocess.CompletedProcess[str]:
        script = PUBLISH_ALONE.format(imports=imports)
        args = [sys.executable, "-c", script, os.environ["KITTUM_DATABASE_URL"], pid, lead]
        return subprocess.run([*args, str(exported)], capture_output=True, text=True, check=False)

    def published() -> list[dict[str, Any]]:
        with database.transaction() as tx:
            events = PostgresLedger().events(tx, pid)
        return [e.payload for e in events if e.type == PUBLISHED_EVENT]

    # Only the protocol's own API: nothing could name the model's fields, so nothing is published.
    alone = publish_alone("")
    assert alone.returncode == 3, alone.stderr
    assert "a model helped write this project's protocol" in alone.stdout
    assert published() == []

    # With the assistant imported, the publication names every field the model wrote.
    with_assist = publish_alone("import kittum.assist")
    assert with_assist.returncode == 0, with_assist.stderr
    (event,) = published()
    assert {p["path"] for p in event["ai_parts"]} == {m["path"] for m in drafted["ai_parts"]}


def test_the_entries_of_lists_without_ids_stay_the_models_when_their_topic_is_renamed(
    pg: Tx,
) -> None:
    db = savepoint_db(pg)
    artifacts = MemoryStore()
    recipe_id, backend = recorded_backend(artifacts)
    ctx = AppContext(
        settings=Settings(database_url="postgresql://unused", artifact_dir=Path("unused")),
        db=db,
        artifacts=artifacts,
        ledger=PostgresLedger(),
        assistant=Assistant(recipe_id=recipe_id, backend=backend),
    )
    client = TestClient(create_app(ctx))
    project = client.post("/api/projects", json={"name": "NRT", "lead_name": "Morgan Lee"}).json()
    pid, lead = project["id"], project["members"][0]["actor_id"]

    # GLM's recorded draft, as if it had also named a known trial and left a source out with a
    # reason: entries of lists without IDs, each with free text and choices.
    answer = assist.draft(backend, recipe_id, DESCRIPTION)
    assert answer.document is not None
    topic = answer.document["search_topics"][0]
    extended = {
        **topic,
        "reference_set": {
            "development": [
                {"kind": "pmid", "value": "20029105", "note": "A pivotal trial of the patch"}
            ],
            "validation": [],
        },
        "source_plan": {
            **topic["source_plan"],
            "deviations": [
                {"source_id": "embase", "justification": {"en": "No subscription to Embase."}}
            ],
        },
    }
    document = compile({**answer.document, "search_topics": [extended]}).protocol
    with db.transaction() as tx:
        outcome = assist.record(
            tx,
            pid,
            lead,
            answer.model_copy(update={"document": document.model_dump(mode="json")}),
            ledger=PostgresLedger(),
        )
    assert outcome.revision is not None
    drafted = outcome.revision.model_dump(mode="json")

    body = {
        "source": {
            **drafted["document"],
            "search_topics": [renamed_topic(drafted["document"]["search_topics"][0], "renamed")],
        },
        "base_id": drafted["id"],
    }
    renamed = client.post(
        f"/api/projects/{pid}/assist/revisions", json=body, headers={"X-Kittum-Reviewer": lead}
    ).json()
    marks = {m["path"]: m["revision_id"] for m in renamed["ai_parts"]}
    for field in (
        "reference_set.development[0].note",
        "reference_set.development[0].value",
        "reference_set.development[0].kind",
        "source_plan.deviations[0].justification.en",
        "source_plan.deviations[0].source_id",
    ):
        assert marks.get(f"search_topics[renamed].{field}") == drafted["id"], field


GLM = """
recipes:
  - id: glm-5.3-flash
    family: glm
    repository: example-org/glm
    served_name: glm-5.3-flash
    revision: "2026-10"
    weights_licence: mit
    licence_url: https://example.org/LICENSE
    serving: hosted
    context_tokens: 131072
"""


def test_a_recorded_assistant_replays_with_the_recipe_it_used_not_the_judges(
    pg: Tx, tmp_path: Path
) -> None:
    """The live run judged with one recipe and drafted with another, recording both
    (`KITTUM_LLM_RECORD`). The replay, told neither, drafts with the assistant's recipe, listed
    second, and gets the same draft; recordings that do not say which recipe the assistant used
    are refused, and so is the shipped demo replay beside a replay of one's own."""
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(GLM)
    answers = json.loads(ANSWERS.read_text())["answers"]

    def server(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        text = "{}"
        if body["model"] == "glm-5.3-flash":  # GLM's recorded answer to this very request
            call = CompletionRequest(
                model_id="glm-5.3-flash",
                messages=tuple(Message(**m) for m in body["messages"]),
                json_schema=body["response_format"]["json_schema"]["schema"],
                temperature=body["temperature"],
                seed=body["seed"],
                max_tokens=body["max_tokens"],
            )
            text = answers[request_key(call)]
        choice = {"message": {"content": text}, "finish_reason": "stop"}
        return httpx2.Response(200, json={"model": body["model"], "choices": [choice]})

    path = tmp_path / "models"
    live = Settings(
        database_url="unused",
        artifact_dir=tmp_path / "artifacts",
        recipes_file=recipes,
        llm_endpoints=(
            LlmEndpoint(recipe_id="qwen3.8-27b-bf16", base_url="http://gpu:8000/v1"),
            LlmEndpoint(recipe_id="glm-5.3-flash", base_url="http://glm.test/v1"),
        ),
        judge_model="qwen3.8-27b-bf16",
        assist_model="glm-5.3-flash",
        llm_record=path,
    )
    registry = ModelRegistry.load(local=recipes)

    def draft_with(settings: Settings, backends: Any, artifacts: Any) -> dict[str, Any]:
        db = savepoint_db(pg)
        ctx = AppContext(
            settings=settings,
            db=db,
            artifacts=artifacts,
            ledger=PostgresLedger(),
            assistant=build_assistant(settings, backends, artifacts),
        )
        client = TestClient(create_app(ctx))
        project = client.post("/api/projects", json={"name": "NRT", "lead_name": "Lee"}).json()
        outcome = client.post(
            f"/api/projects/{project['id']}/assist/draft",
            json={"description": DESCRIPTION},
            headers={"X-Kittum-Reviewer": project["members"][0]["actor_id"]},
        ).json()
        assert outcome["revision"], outcome["issues"]
        return outcome["revision"]

    artifacts = FilesystemStore(live.artifact_dir)
    client = httpx2.Client(transport=httpx2.MockTransport(server))
    backends = build_backends(live, registry, artifacts, client=client)
    assert build_judge(live, registry, backends) is not None  # notes the judge's role
    judged = Message(role="user", content="Read this record.")
    backends["qwen3.8-27b-bf16"].complete(
        CompletionRequest(model_id="qwen3.8-27b-bf16", messages=(judged,))
    )
    drafted = draft_with(live, backends, artifacts)

    replay = replace(live, llm_record=None, llm_replay=path, judge_model=None, assist_model=None)
    store = FilesystemStore(tmp_path / "replayed")
    replays = build_backends(replay, registry, store)
    assert list(replays) == ["qwen3.8-27b-bf16", "glm-5.3-flash"]
    replayed = draft_with(replay, replays, store)
    assert replayed["document"] == drafted["document"]
    assert replayed["model"]["recipe_id"] == "glm-5.3-flash"
    assert replayed["model"]["endpoint"] == "replay of http://glm.test/v1"

    meta = json.loads((path / "meta.json").read_text())
    assert meta["roles"] == {"judge": "qwen3.8-27b-bf16", "assist": "glm-5.3-flash"}
    del meta["roles"]["assist"]
    (path / "meta.json").write_text(json.dumps(meta))
    with pytest.raises(ConfigurationError, match="which recipe the protocol assistant used"):
        build_assistant(replay, replays, store)
    with pytest.raises(
        ConfigurationError, match=r"KITTUM_ASSIST_BACKEND=demo .* KITTUM_LLM_REPLAY"
    ):
        build_assistant(replace(replay, assist_backend="demo"), replays, store)
