"""A judge run recorded from a hosted model server, then replayed without one.

The server needs a key, which must not reach a recording. The replay
must give the same verdicts from the same model identity, put the exact request and response bytes
back where every hash points, mark each completion as replayed, and fail on a request it never saw,
naming that request's hash.
"""

import json
from typing import TYPE_CHECKING

import httpx2
import pytest

from kittum.judge import applicable, build_request, run_judge
from kittum.models import (
    LlmRecordings,
    ModelRegistry,
    NotReplayed,
    OpenAICompatibleBackend,
    RecordingBackend,
    ReplayBackend,
    request_key,
)
from kittum.storage import MemoryStore
from tests.judge.records import (
    ADOLESCENTS,
    ANSWERS,
    CHEMO_NAIVE,
    CHEMO_NAIVE_AGED,
    CONFIG,
    CRITERIA,
    EXCLUDED_CHEMO_NAIVE,
    INJECTED,
    SCOPE,
)

if TYPE_CHECKING:
    from pathlib import Path

ITEMS = (ADOLESCENTS, EXCLUDED_CHEMO_NAIVE, CHEMO_NAIVE, INJECTED)


class HostedJudge:
    """Answers each record with its recorded answer, as a server that names its own model."""

    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []
        self.answers = {it.title: ANSWERS[it.item_ref] for it in ITEMS}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        user = json.loads(request.content)["messages"][-1]["content"]
        text = next(a for title, a in self.answers.items() if title in user)
        message = {"role": "assistant", "content": text}
        return httpx2.Response(
            200,
            json={
                "model": "Qwen/Qwen3.8-27B-2026-10",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )


@pytest.mark.parametrize("name", ["judge.json", "judge-recordings"])
def test_a_recorded_judge_run_replays_to_the_same_verdicts_and_says_it_was_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    key = "sk-live-0123456789abcdef"
    monkeypatch.setenv("JUDGE_KEY", key)
    recipe = ModelRegistry.load().get(CONFIG.model_id)
    server = HostedJudge()
    live_store = MemoryStore()
    live = OpenAICompatibleBackend(
        recipe,
        "https://llm.example/v1/",
        live_store,
        api_key_env="JUDGE_KEY",
        client=httpx2.Client(transport=httpx2.MockTransport(server)),
    )
    path = tmp_path / name
    recording = RecordingBackend(live, recipe, LlmRecordings(path), live_store)
    recorded = [run_judge(it, SCOPE, CRITERIA, CONFIG, recording) for it in ITEMS]
    assert len(server.seen) == len(ITEMS)
    assert all(r.headers["authorization"] == f"Bearer {key}" for r in server.seen)

    # Nothing written holds the key.
    written = [path] if path.is_file() else sorted(path.iterdir())
    assert len(written) == (1 if path.is_file() else len(ITEMS))
    for file in written:
        content = file.read_text()
        assert key not in content and "authorization" not in content.lower()

    # A fresh deployment: no server, no key, an empty artifact store.
    monkeypatch.delenv("JUDGE_KEY")
    replay_store = MemoryStore()
    replay = ReplayBackend(recipe, LlmRecordings(path), replay_store)
    replayed = [run_judge(it, SCOPE, CRITERIA, CONFIG, replay) for it in ITEMS]
    assert len(server.seen) == len(ITEMS)

    assert [r.verdict for r in replayed] == [r.verdict for r in recorded]
    # The answer that excluded still excludes, the injected one still does not.
    assert [r.verdict.excludes for r in replayed] == [False, False, True, False]
    assert {r.verdict.model_identity for r in replayed} == {"Qwen/Qwen3.8-27B-2026-10"}
    for live_run, replay_run in zip(recorded, replayed, strict=True):
        (was,) = live_run.attempts
        (now,) = replay_run.attempts
        assert was.completion.endpoint == "https://llm.example/v1"
        assert now.completion.endpoint == "replay of https://llm.example/v1"
        assert now.completion.serving == was.completion.serving == recipe.serving
        # Everything but the endpoint is the live completion, and every hash resolves.
        assert now.completion.model_copy(update={"endpoint": was.completion.endpoint}) == (
            was.completion
        )
        assert now.completion.raw_request_hash is not None
        assert replay_store.get(now.completion.raw_request_hash) == live_store.get(
            now.completion.raw_request_hash
        )
        assert replay_store.get(now.completion.raw_response_hash) == live_store.get(
            now.completion.raw_response_hash
        )

    # A record the live run never judged: the replay names the request it lacks.
    missing = build_request(CHEMO_NAIVE_AGED, applicable(CRITERIA, SCOPE, CHEMO_NAIVE_AGED), CONFIG)
    with pytest.raises(NotReplayed, match=request_key(missing)):
        run_judge(CHEMO_NAIVE_AGED, SCOPE, CRITERIA, CONFIG, replay)
