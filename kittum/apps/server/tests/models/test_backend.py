"""The OpenAI-compatible backend against recorded vLLM and Ollama response bodies."""

import json
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest

from kittum.core import NotFoundError
from kittum.models import (
    BackendError,
    CompletionRequest,
    Message,
    ModelRegistry,
    OpenAICompatibleBackend,
    backends_from_endpoints,
    request_key,
)
from kittum.settings import LlmEndpoint
from kittum.storage import MemoryStore
from tests.models.streaming import StreamingServer, chunks, event

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMA = {
    "type": "object",
    "properties": {
        "C_POP_ADULT_NSCLC": {
            "type": "object",
            "properties": {
                "quote": {"type": "string"},
                "rationale": {"type": "string"},
                "status": {"enum": ["met", "not_met", "unclear"], "type": "string"},
            },
            "required": ["quote", "rationale", "status"],
            "additionalProperties": False,
        }
    },
    "required": ["C_POP_ADULT_NSCLC"],
    "additionalProperties": False,
}
ANSWER = {
    "C_POP_ADULT_NSCLC": {
        "quote": "patients aged 12 to 17 years",
        "rationale": "The trial enrolled adolescents, not adults.",
        "status": "not_met",
    }
}


class RecordingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__()
        self.puts: list[bytes] = []

    def put(self, data: bytes) -> str:
        self.puts.append(data)
        return super().put(data)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _request(model_id: str, **kwargs: Any) -> CompletionRequest:
    return CompletionRequest(
        model_id=model_id,
        messages=(
            Message(role="system", content="You screen records."),
            Message(role="user", content="Record: 48 patients aged 12 to 17 years ..."),
        ),
        json_schema=SCHEMA,
        seed=7,
        **kwargs,
    )


class Server:
    """Answers each POST with the next scripted reply and records what was sent."""

    def __init__(self, *replies: bytes | int | Exception | httpx2.Response) -> None:
        self.replies = list(replies)
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        if isinstance(reply, int):
            return httpx2.Response(reply, json={"object": "error", "message": "engine busy"})
        return httpx2.Response(200, content=reply, headers={"content-type": "application/json"})

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self))


@pytest.fixture(scope="module")
def registry() -> ModelRegistry:
    return ModelRegistry.load()


def test_vllm_call_is_constrained_deterministic_and_stored(registry: ModelRegistry) -> None:
    server = Server(_fixture("vllm_qwen_structured.json"))
    store = MemoryStore()
    endpoints = (LlmEndpoint(recipe_id="qwen3.8-27b-bf16", base_url="http://gpu-box:8000/v1/"),)
    backend = backends_from_endpoints(endpoints, registry, store, client=server.client())[
        "qwen3.8-27b-bf16"
    ]

    request = _request("qwen3.8-27b-bf16")
    completion = backend.complete(request)

    (sent,) = server.seen
    assert str(sent.url) == "http://gpu-box:8000/v1/chat/completions"
    assert "authorization" not in sent.headers  # a local server needs no key
    body = json.loads(sent.content)
    assert body["model"] == "Qwen/Qwen3.8-27B"
    assert body["temperature"] == 0.0
    assert body["seed"] == 7
    # Kittum asks for a stream; this server sent one JSON body, which reads the same.
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "kittum_answer", "schema": SCHEMA, "strict": True},
    }
    assert body["chat_template_kwargs"] == {"enable_thinking": False}

    assert json.loads(completion.text) == ANSWER
    assert completion.model_identity == "Qwen/Qwen3.8-27B"
    assert completion.finish_reason == "stop"
    assert completion.reasoning is None
    assert completion.request_hash == request_key(request)
    assert (completion.serving, completion.endpoint) == ("vllm", "http://gpu-box:8000/v1")
    assert completion.raw_request_hash is not None
    assert store.get(completion.raw_request_hash) == sent.content
    assert store.get(completion.raw_response_hash) == _fixture("vllm_qwen_structured.json")


def test_served_name_is_sent_and_the_reported_identity_kept(registry: ModelRegistry) -> None:
    server = Server(_fixture("vllm_nemotron_reasoning_parser.json"))
    backend = OpenAICompatibleBackend(
        registry.get("nemotron-3.5-super-vl-nvfp4"),
        "http://h200:8000/v1",
        MemoryStore(),
        client=server.client(),
    )
    completion = backend.complete(_request("nemotron-3.5-super-vl-nvfp4"))
    assert json.loads(server.seen[0].content)["model"] == "nemotron-3.5-super-vl"
    assert completion.model_identity == "nemotron-3.5-super-vl"
    assert json.loads(completion.text) == ANSWER
    assert completion.reasoning is not None
    assert "12 to 17 years" in completion.reasoning


@pytest.mark.parametrize(
    ("recipe_id", "fixture", "model", "extra"),
    [
        (
            "qwen3.8-27b-bf16-thinking",
            "vllm_qwen_thinking_unparsed.json",
            "Qwen/Qwen3.8-27B",
            {"chat_template_kwargs": {"enable_thinking": True}, "reasoning_effort": "medium"},
        ),
        (
            "qwen3.5-4b-ollama",
            "ollama_qwen_thinking.json",
            "qwen3.5:4b",
            {"reasoning_effort": "none"},
        ),
        ("gemma-4-e4b-it-ollama", "ollama_gemma_structured.json", "gemma4:e4b", {}),
    ],
)
def test_thinking_and_fences_are_removed_before_the_json(
    registry: ModelRegistry, recipe_id: str, fixture: str, model: str, extra: dict[str, Any]
) -> None:
    server = Server(_fixture(fixture))
    backend = OpenAICompatibleBackend(
        registry.get(recipe_id), "http://127.0.0.1:11434/v1", MemoryStore(), client=server.client()
    )
    completion = backend.complete(_request(recipe_id))
    body = json.loads(server.seen[0].content)
    assert body["model"] == model
    assert {k: body[k] for k in extra} == extra
    assert json.loads(completion.text) == ANSWER
    assert completion.model_identity == model
    if registry.get(recipe_id).reasoning:
        assert completion.reasoning is not None
        assert "12 to 17" in completion.reasoning


@pytest.mark.parametrize(
    "content",
    [
        "<think>\nAdults only; these are adolescents.\n</think>\n\n{json}",
        "<|channel>thought\n<channel|>{json}",
        "<|channel|>analysis<|message|>Adolescents.<|end|><|start|>assistant"
        "<|channel|>final<|message|>{json}<|return|>",
        "```json\n{json}\n```",
    ],
)
def test_raw_thinking_formats_of_each_family_are_stripped(content: str) -> None:
    body = json.loads(_fixture("ollama_gemma_structured.json"))
    body["choices"][0]["message"]["content"] = content.replace("{json}", json.dumps(ANSWER))
    server = Server(json.dumps(body).encode())
    backend = OpenAICompatibleBackend(
        ModelRegistry.load().get("gemma-4-e4b-it-ollama"),
        "http://127.0.0.1:11434/v1",
        MemoryStore(),
        client=server.client(),
    )
    assert json.loads(backend.complete(_request("gemma-4-e4b-it-ollama")).text) == ANSWER


def test_transport_errors_are_retried_and_http_errors_are_not(registry: ModelRegistry) -> None:
    recipe = registry.get("gemma-4-31b-it-bf16")
    flaky = Server(
        httpx2.ConnectError("connection refused"),
        httpx2.ReadTimeout("timed out"),
        _fixture("vllm_qwen_structured.json"),
    )
    backend = OpenAICompatibleBackend(
        recipe, "http://gpu:8000/v1", MemoryStore(), client=flaky.client(), wait_initial=0
    )
    assert json.loads(backend.complete(_request(recipe.id)).text) == ANSWER
    assert len(flaky.seen) == 3
    assert len({r.content for r in flaky.seen}) == 1

    store = RecordingStore()
    busy = Server(503, _fixture("vllm_qwen_structured.json"))
    backend = OpenAICompatibleBackend(
        recipe, "http://gpu:8000/v1", store, client=busy.client(), wait_initial=0
    )
    with pytest.raises(BackendError, match="HTTP 503") as busy_error:
        backend.complete(_request(recipe.id))
    assert len(busy.seen) == 1
    assert b"engine busy" in store.get(busy_error.value.raw_response_hash or "")
    assert store.get(busy_error.value.raw_request_hash or "") == busy.seen[0].content

    # A server that stays down is a backend error too, naming the request that never got an answer.
    store = RecordingStore()
    down = Server(*(httpx2.ConnectError("refused") for _ in range(3)))
    backend = OpenAICompatibleBackend(
        recipe, "http://gpu:8000/v1", store, client=down.client(), wait_initial=0
    )
    with pytest.raises(BackendError, match="unreachable: ConnectError") as down_error:
        backend.complete(_request(recipe.id))
    assert len(down.seen) == 3
    assert down_error.value.raw_response_hash is None
    assert store.get(down_error.value.raw_request_hash or "") == down.seen[0].content


def test_a_hosted_api_gets_its_key_from_the_environment_and_the_recipe_unchanged(
    registry: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hosted reasoning model thinks by default: the recipe's switch goes out as written, and
    thinking that comes back anyway, in its own field or before the JSON, never reaches the
    answer."""
    hosted = registry.get("gemma-4-31b-it-bf16").model_copy(
        update={
            "id": "example-hosted",
            "serving": "hosted",
            "served_name": "example-model",
            "reasoning": True,
            "extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "none"},
        }
    )
    body = json.loads(_fixture("vllm_qwen_structured.json"))
    message = body["choices"][0]["message"]
    message["reasoning_content"] = "The record says 12 to 17 years."
    message["content"] = "<think>Adolescents.</think>\n" + message["content"]
    server = Server(json.dumps(body).encode(), json.dumps(body).encode())
    store = RecordingStore()
    (backend,) = backends_from_endpoints(
        (
            LlmEndpoint(
                recipe_id="example-hosted",
                base_url="https://llm.example/v1/",
                api_key_env="EXAMPLE_LLM_KEY",
            ),
        ),
        ModelRegistry([hosted]),
        store,
        client=server.client(),
    ).values()

    # Without the variable nothing is sent, and the error names the variable.
    monkeypatch.delenv("EXAMPLE_LLM_KEY", raising=False)
    with pytest.raises(BackendError, match="EXAMPLE_LLM_KEY is not set"):
        backend.complete(_request("example-hosted"))
    assert server.seen == [] and store.puts == []

    # The key is read for each call, so a rotated key needs no restart.
    for key in ("sk-first-0000", "sk-rotated-1111"):
        monkeypatch.setenv("EXAMPLE_LLM_KEY", key)
        completion = backend.complete(_request("example-hosted"))
        sent = server.seen[-1]
        assert sent.headers["authorization"] == f"Bearer {key}"
        assert not any(key.encode() in data for data in store.puts)

    assert str(sent.url) == "https://llm.example/v1/chat/completions"
    sent_body = json.loads(sent.content)
    assert sent_body["thinking"] == {"type": "disabled"}
    assert sent_body["reasoning_effort"] == "none"
    assert sent_body["model"] == "example-model"
    assert json.loads(completion.text) == ANSWER
    assert completion.reasoning == "The record says 12 to 17 years."
    assert (completion.serving, completion.endpoint) == ("hosted", "https://llm.example/v1")
    assert store.get(completion.raw_request_hash or "") == sent.content

    # A provider that echoes a wrong key in its error gets it redacted, in the error and the store.
    monkeypatch.setenv("EXAMPLE_LLM_KEY", "sk-wrong-2222")
    server.replies.append(
        httpx2.Response(401, json={"error": {"message": "Invalid API key: sk-wrong-2222"}})
    )
    with pytest.raises(BackendError, match=r"HTTP 401: .*Invalid API key: \[redacted\]") as e:
        backend.complete(_request("example-hosted"))
    assert "sk-wrong-2222" not in str(e.value)
    assert not any(b"sk-wrong-2222" in data for data in store.puts)

    # A key with a stray newline is refused before sending, without its value in the error.
    sent_before = len(server.seen)
    monkeypatch.setenv("EXAMPLE_LLM_KEY", "sk-newline-3333\n")
    with pytest.raises(BackendError, match="EXAMPLE_LLM_KEY is not a valid API key") as e:
        backend.complete(_request("example-hosted"))
    assert "sk-newline-3333" not in str(e.value) and len(server.seen) == sent_before

    # The recorded endpoint drops credentials and the query, and keeps an IPv6 host bracketed.
    (local,) = backends_from_endpoints(
        (LlmEndpoint(recipe_id="example-hosted", base_url="http://user:pw@[::1]:8000/v1/?k=1"),),
        ModelRegistry([hosted]),
        store,
        client=server.client(),
    ).values()
    assert local.endpoint == "http://[::1]:8000/v1"


def test_a_backend_refuses_requests_for_another_recipe(registry: ModelRegistry) -> None:
    server = Server()
    backend = OpenAICompatibleBackend(
        registry.get("gemma-4-31b-it-bf16"), "http://gpu/v1", MemoryStore(), client=server.client()
    )
    with pytest.raises(ValueError, match="serves gemma-4-31b-it-bf16"):
        backend.complete(_request("qwen3.8-27b-bf16"))
    assert server.seen == []


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b'{"object": "chat.completion", "choices": []}', "list index out of range"),
        # A stream that stops before the answer does, and ones that report an error midway: in a
        # data payload (OpenAI) or as an error event after an answer delta, the stream ending
        # as if nothing happened.
        (
            b"".join(chunks(model="gemma", reasoning=["Adults?"], content=['{"C_', "POP"])[:2]),
            "the stream ended before the answer did",
        ),
        (
            chunks(model="gemma", reasoning=["Adults?"], content=["{"])[0]
            + event({"error": {"message": "overloaded", "code": "1305"}}),
            "the server reported an error",
        ),
        (
            b"".join(chunks(model="gemma", reasoning=["Adults?"], content=['{"C_', "POP"])[:2])
            + b'event: error\ndata: {"message": "overloaded", "code": 500}\n\n'
            + event("[DONE]"),
            "the server reported an error",
        ),
    ],
)
def test_a_malformed_body_is_stored_and_raised(
    registry: ModelRegistry, body: bytes, why: str
) -> None:
    store = RecordingStore()
    server = Server(body)
    backend = OpenAICompatibleBackend(
        registry.get("gemma-4-31b-it-bf16"), "http://gpu/v1", store, client=server.client()
    )
    with pytest.raises(BackendError, match=f"not a chat completion: {why}") as error:
        backend.complete(_request("gemma-4-31b-it-bf16"))
    assert store.get(error.value.raw_response_hash or "") == body


def test_a_slow_steady_stream_outlasts_the_read_timeout_and_a_silent_one_does_not(
    registry: ModelRegistry,
) -> None:
    """The read timeout counts the silence between chunks, not the whole answer. A model that
    thinks for three times the timeout, sending as it goes, answers; one that stops sending for
    longer than the timeout fails, its request and what it sent before it went silent on
    record."""
    recipe = registry.get("qwen3.8-27b-bf16")
    answer = json.dumps(ANSWER)
    steady = chunks(
        model="Qwen/Qwen3.8-27B",
        reasoning=[f"Step {n} of the reading. " for n in range(1, 9)],
        content=[answer[:40], answer[40:]],
    )
    with StreamingServer(lambda _: [step for c in steady for step in (c, 0.15)]) as server:
        backend = OpenAICompatibleBackend(
            recipe, server.url, RecordingStore(), timeout=0.5, attempts=1
        )
        started = time.monotonic()
        completion = backend.complete(_request(recipe.id))
        assert time.monotonic() - started > 1.5  # three times the timeout
    assert json.loads(completion.text) == ANSWER
    assert completion.reasoning == "".join(f"Step {n} of the reading. " for n in range(1, 9))
    assert completion.finish_reason == "stop"

    store = RecordingStore()
    with StreamingServer(lambda _: [steady[0], 1.0, *steady[1:]]) as server:
        backend = OpenAICompatibleBackend(recipe, server.url, store, timeout=0.5, attempts=1)
        with pytest.raises(BackendError, match=r"model server sent nothing for 0\.5 s") as error:
            backend.complete(_request(recipe.id))
    assert store.get(error.value.raw_response_hash or "") == steady[0]
    assert store.get(error.value.raw_request_hash or "") == server.seen[0]


def test_unknown_endpoint_recipes_fail_at_startup(registry: ModelRegistry) -> None:
    with pytest.raises(NotFoundError, match=r"llama-3\.3-70b"):
        backends_from_endpoints(
            (LlmEndpoint(recipe_id="llama-3.3-70b", base_url="http://gpu/v1"),),
            registry,
            MemoryStore(),
        )
