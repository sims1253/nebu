"""What is behind the hashes of an AI step, over HTTP.

The judge reads the screening records through Kittum's live OpenAI-compatible backend, against a
server that answers like vLLM with its reasoning parser on. The methods lead then traces a
record: the judge's event says in words what the AI proposed, on which criterion, with which
quote; its model call opens to the exact request body the server got and the exact response it
sent, reasoning included; Kittum hashes each again and says whether it still matches. An
artifact changed in the store no longer matches. The AI disclosure prints the prompts and the
request settings read from the same stored request. Only the lead may ask.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.settings import Settings
from tests.report.support import OverHttp
from tests.screening.support import (
    RECIPE,
    SENTINELS,
    build_world,
    recorded_backend,
)

if TYPE_CHECKING:
    from kittum.db import Database

pytestmark = pytest.mark.postgres

REASONING = "The abstract reports adult daily smokers, so the population criterion is met."


def test_an_ai_step_opens_to_the_exact_request_and_response_and_its_prompts_are_disclosed(
    database: Database,
) -> None:
    world = build_world(database)
    server = OverHttp(
        recorded_backend([*world.records, *(s.record for s in SENTINELS)], world.criteria()),
        RECIPE,
        world.artifacts,
        reasoning=REASONING,
    )
    world.open(backend=server)  # type: ignore[arg-type]
    assert server.seen, "the judge sent nothing over HTTP"

    context = AppContext(
        settings=Settings(database_url="postgresql://unused", artifact_dir=Path("unused")),
        db=world.db,
        artifacts=world.artifacts,
        ledger=world.ledger,
    )
    client = TestClient(create_app(context))
    base = f"/api/projects/{world.project_id}"
    lead = {"X-Kittum-Reviewer": str(world.people.lead)}

    # The lead traces a record the AI proposed to exclude: the colitis trial's non-smokers.
    record = next(r for r in world.records if r.title.startswith("Transdermal nicotine"))
    trace = client.get(f"{base}/audit/records/{record.item_ref}/trace", headers=lead)
    assert trace.status_code == 200, trace.text
    events = trace.json()["events"]
    (judged,) = [e for e in events if e["type"] == "judge.verdict_recorded"]

    # First what happened, in words: the proposal, the criterion's wording, the quote.
    assert judged["summary"] == "The AI judge proposed to exclude the record on POP"
    facts = {f["label"]: f["value"] for f in judged["facts"]}
    (population,) = [label for label in facts if label.startswith("POP. ")]
    assert facts[population].startswith("Not met.")
    assert 'Quote "' in facts[population]
    assert facts["Shown to screeners"].startswith("Only after their own decision")

    # Then the hashes, each with the content behind it and Kittum's check of it.
    for e in events:
        assert e["payload_hash_matches"] and e["event_hash_matches"], e["seq"]
        assert e["hashed_fields"]["payload_hash"] == e["payload_hash"]
        assert e["hashed_fields"]["prev_hash"] == e["prev_hash"]

    (call,) = judged["model_calls"]
    assert (call["request"]["form"], call["response"]["form"]) == ("http_body", "http_response")
    assert call["request"]["stored"] and call["response"]["stored"]
    assert call["model_identity"] == RECIPE.api_model and call["finish_reason"] == "stop"

    def artifact(sha256: str) -> bytes:
        got = client.get(f"{base}/audit/artifacts/{sha256}", headers=lead)
        assert got.status_code == 200, got.text
        return got.content

    sent = artifact(call["request"]["sha256"])
    assert sent in [r.content for r in server.seen]  # byte for byte what the server got
    body = json.loads(sent)
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert f"Title: {record.title}" in body["messages"][1]["content"]
    assert body["chat_template_kwargs"] == RECIPE.extra_body["chat_template_kwargs"]
    received = artifact(call["response"]["sha256"])
    assert received in server.replies
    message = json.loads(received)["choices"][0]["message"]
    assert message["reasoning_content"] == REASONING
    assert json.loads(message["content"])["POP"]["status"] == "not_met"

    checked = client.get(f"{base}/audit/artifacts/{call['request']['sha256']}/check", headers=lead)
    assert checked.status_code == 200, checked.text
    assert checked.json() | {"media_type": None} == {
        "sha256": call["request"]["sha256"],
        "stored": True,
        "bytes": len(sent),
        "recomputed": call["request"]["sha256"],
        "matches": True,
        "media_type": None,
    }
    # Bytes changed in the store no longer hash to what the ledger points at.
    world.artifacts._data[call["response"]["sha256"]] = received.replace(b"not_met", b"met")
    changed = client.get(
        f"{base}/audit/artifacts/{call['response']['sha256']}/check", headers=lead
    ).json()
    assert changed["stored"] and not changed["matches"]
    assert changed["recomputed"] != call["response"]["sha256"]
    world.artifacts._data[call["response"]["sha256"]] = received

    # The disclosure prints what the judge sent, read from the same stored requests.
    rendered = client.post(
        f"{base}/reports/targets/ai_disclosure/render",
        json={"actor_id": str(world.people.lead)},
    )
    assert rendered.status_code == 200, rendered.text
    files = {
        f["path"]: client.get(f"{base}/reports/artifacts/{f['sha256']}", headers=lead).content
        for f in rendered.json()["artifact"]["files"]
    }
    (card,) = json.loads(files["ai_disclosure.json"])["prompts"]
    first = json.loads(server.seen[0].content)
    assert (card["role"], card["request_form"]) == ("judge", "http_body")
    assert card["system_prompt"] == first["messages"][0]["content"]
    assert card["json_schema"] == first["response_format"]["json_schema"]["schema"]
    assert card["request_settings"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert {"temperature", "max_tokens", "seed"} <= set(card["request_settings"])
    assert card["model"]["revision"] == RECIPE.revision
    assert card["calls"] == len(server.seen)
    text = files["ai_disclosure.md"].decode()
    assert first["messages"][0]["content"] in text
    assert '"enable_thinking": false' in text

    # Only the lead sees any of it: a screener is refused, and nothing unnamed can be fetched.
    screener = {"X-Kittum-Reviewer": str(world.people.a)}
    for path in (
        f"audit/artifacts/{call['request']['sha256']}",
        f"audit/artifacts/{call['request']['sha256']}/check",
    ):
        assert client.get(f"{base}/{path}", headers=screener).status_code == 403, path
    unnamed = client.get(f"{base}/audit/artifacts/{'0' * 64}/check", headers=lead)
    assert unnamed.status_code == 404
