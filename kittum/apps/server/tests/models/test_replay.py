"""A judge run recorded from a hosted model server, then replayed without one.

The server needs a key, which must not reach a recording. The replay must give the same verdicts
from the same model identity, in the same order when a record is judged twice and the model
answers differently the second time, put the exact request and response bytes back where every
hash points, mark each completion as replayed, and fail on a request it has no answer left for,
naming that request's hash. A completion whose request or bytes hold a key or the contact address
is not recorded at all.
"""

import json
from typing import TYPE_CHECKING

import httpx2
import pytest

from kittum.judge import ScreeningItem, applicable, build_request, run_judge
from kittum.models import (
    LlmRecordingRefused,
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
    answer,
)

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.models import ModelRecipe

ITEMS = (ADOLESCENTS, EXCLUDED_CHEMO_NAIVE, CHEMO_NAIVE, INJECTED)
KEY = "sk-live-0123456789abcdef"
CONTACT = "max.mustermann@uni-example.de"
# What the model says when it reads a record a second time: it no longer sees the exclusion.
SECOND_READING = answer(
    C_POP_ADULT_NSCLC=("unclear", "", "Not clear on a second reading."),
    C_PRIOR_CHEMO=("unclear", "", "Not clear on a second reading."),
    C_RCT=("met", "double-blind, randomised phase 3 trial", "The study is randomised."),
)


class HostedJudge:
    """Answers each record with its recorded answer the first time and `SECOND_READING` after, as
    a server that names its own model."""

    def __init__(self, items: tuple[ScreeningItem, ...] = ITEMS) -> None:
        self.seen: list[httpx2.Request] = []
        self.answers = {it.title: ANSWERS.get(it.item_ref, SECOND_READING) for it in items}
        self.read: set[str] = set()

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        user = json.loads(request.content)["messages"][-1]["content"]
        title = next(t for t in self.answers if t in user)
        text = SECOND_READING if title in self.read else self.answers[title]
        self.read.add(title)
        message = {"role": "assistant", "content": text}
        return httpx2.Response(
            200,
            json={
                "model": "Qwen/Qwen3.8-27B-2026-10",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )


def _live(
    recipe: ModelRecipe, server: HostedJudge, store: MemoryStore, path: Path
) -> RecordingBackend:
    backend = OpenAICompatibleBackend(
        recipe,
        "https://llm.example/v1/",
        store,
        api_key_env="JUDGE_KEY",
        client=httpx2.Client(transport=httpx2.MockTransport(server)),
    )
    return RecordingBackend(backend, recipe, LlmRecordings(path), store, sensitive=(KEY, CONTACT))


@pytest.mark.parametrize("name", ["judge.json", "judge-recordings"])
def test_a_recorded_judge_run_replays_to_the_same_verdicts_and_says_it_was_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("JUDGE_KEY", KEY)
    recipe = ModelRegistry.load().get(CONFIG.model_id)
    server = HostedJudge()
    live_store = MemoryStore()
    path = tmp_path / name
    recording = _live(recipe, server, live_store, path)
    # The last record is judged again, and the model reads it differently.
    order = (*ITEMS, CHEMO_NAIVE)
    recorded = [run_judge(it, SCOPE, CRITERIA, CONFIG, recording) for it in order]
    assert len(server.seen) == len(order)
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in server.seen)
    assert [r.verdict.excludes for r in recorded] == [False, False, True, False, False]

    # Nothing written holds the key: one file, or one per distinct request.
    written = [path] if path.is_file() else sorted(path.glob("[!.]*"))
    assert len(written) == (1 if path.is_file() else len(ITEMS))
    for file in written:
        content = file.read_text()
        assert KEY not in content and "authorization" not in content.lower()

    # A fresh deployment: no server, no key, an empty artifact store.
    monkeypatch.delenv("JUDGE_KEY")
    replay_store = MemoryStore()
    replay = ReplayBackend(recipe, LlmRecordings(path), replay_store)
    replayed = [run_judge(it, SCOPE, CRITERIA, CONFIG, replay) for it in order]
    assert len(server.seen) == len(order)

    # The same verdicts in the same order: the record judged twice excludes, then does not.
    assert [r.verdict for r in replayed] == [r.verdict for r in recorded]
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

    # A third reading was never recorded, nor was a record the live run never judged.
    third = build_request(CHEMO_NAIVE, applicable(CRITERIA, SCOPE, CHEMO_NAIVE), CONFIG)
    with pytest.raises(NotReplayed, match=f"completion 3 for request {request_key(third)}"):
        run_judge(CHEMO_NAIVE, SCOPE, CRITERIA, CONFIG, replay)
    missing = build_request(CHEMO_NAIVE_AGED, applicable(CRITERIA, SCOPE, CHEMO_NAIVE_AGED), CONFIG)
    with pytest.raises(NotReplayed, match=f"completion 1 for request {request_key(missing)}"):
        run_judge(CHEMO_NAIVE_AGED, SCOPE, CRITERIA, CONFIG, replay)


def test_a_completion_holding_a_key_or_the_contact_address_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that names the contact address goes into the prompt; a recipe whose extra body
    carries the key sends it in the request bytes. Neither may be written, nor scrubbed (the
    recorded hashes would no longer match), so recording refuses and says where, not what."""
    monkeypatch.setenv("JUDGE_KEY", KEY)
    recipe = ModelRegistry.load().get(CONFIG.model_id)
    path = tmp_path / "judge-recordings"
    mailed = ScreeningItem(
        item_ref="occ-mailed",
        stage="title_abstract",
        title=CHEMO_NAIVE.title,
        text=f"{CHEMO_NAIVE.text}\nCorrespondence: {CONTACT.upper()}",
    )
    server = HostedJudge((mailed,))
    with pytest.raises(LlmRecordingRefused, match="its request holds") as refused:
        run_judge(mailed, SCOPE, CRITERIA, CONFIG, _live(recipe, server, MemoryStore(), path))
    assert CONTACT not in str(refused.value).lower()

    keyed = recipe.model_copy(update={"extra_body": {**recipe.extra_body, "api_token": KEY}})
    server = HostedJudge()
    with pytest.raises(LlmRecordingRefused, match="its raw request holds") as refused:
        run_judge(CHEMO_NAIVE, SCOPE, CRITERIA, CONFIG, _live(keyed, server, MemoryStore(), path))
    assert KEY not in str(refused.value)

    # Both were asked, and neither left a trace.
    assert len(server.seen) == 1
    assert not path.exists() or not list(path.glob("[!.]*"))
