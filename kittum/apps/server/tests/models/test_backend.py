"""The OpenAI-compatible backend against recorded vLLM and Ollama response bodies."""

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from kittum.core import NotFoundError
from kittum.models import (
    BackendError,
    CompletionRequest,
    Confidentiality,
    ConfidentialityError,
    Message,
    ModelRegistry,
    OpenAICompatibleBackend,
    backends_from_endpoints,
    request_key,
)
from kittum.settings import LlmEndpoint
from kittum.storage import MemoryStore

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

    def __init__(self, *replies: bytes | int | Exception) -> None:
        self.replies = list(replies)
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
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
    endpoints = (
        LlmEndpoint(recipe_id="qwen3.8-27b-bf16", base_url="http://gpu-box:8000/v1/", api_key="k"),
    )
    backend = backends_from_endpoints(endpoints, registry, store, client=server.client())[
        "qwen3.8-27b-bf16"
    ]

    request = _request("qwen3.8-27b-bf16")
    completion = backend.complete(request)

    (sent,) = server.seen
    assert str(sent.url) == "http://gpu-box:8000/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer k"
    body = json.loads(sent.content)
    assert body["model"] == "Qwen/Qwen3.8-27B"
    assert body["temperature"] == 0.0
    assert body["seed"] == 7
    assert body["stream"] is False
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
    with pytest.raises(BackendError, match="HTTP 503"):
        backend.complete(_request(recipe.id))
    assert len(busy.seen) == 1
    assert any(b"engine busy" in data for data in store.puts)

    down = Server(*(httpx2.ConnectError("refused") for _ in range(3)))
    backend = OpenAICompatibleBackend(
        recipe, "http://gpu:8000/v1", MemoryStore(), client=down.client(), wait_initial=0
    )
    with pytest.raises(httpx2.ConnectError):
        backend.complete(_request(recipe.id))
    assert len(down.seen) == 3


def test_local_only_content_never_reaches_a_hosted_model(registry: ModelRegistry) -> None:
    hosted = registry.get("gemma-4-31b-it-bf16").model_copy(
        update={"id": "gemma-4-31b-eu-hosted", "serving": "hosted", "served_name": "gemma-4-31b"}
    )
    server = Server(_fixture("vllm_qwen_structured.json"))
    store = RecordingStore()
    backend = OpenAICompatibleBackend(
        hosted, "https://eu.example/v1", store, client=server.client()
    )

    with pytest.raises(ConfidentialityError):
        backend.complete(_request(hosted.id))  # local_only is the default
    assert server.seen == []
    assert store.puts == []

    completion = backend.complete(_request(hosted.id, confidentiality=Confidentiality.CONFIDENTIAL))
    assert json.loads(completion.text) == ANSWER
    assert len(server.seen) == 1


def test_a_backend_refuses_requests_for_another_recipe(registry: ModelRegistry) -> None:
    server = Server()
    backend = OpenAICompatibleBackend(
        registry.get("gemma-4-31b-it-bf16"), "http://gpu/v1", MemoryStore(), client=server.client()
    )
    with pytest.raises(ValueError, match="serves gemma-4-31b-it-bf16"):
        backend.complete(_request("qwen3.8-27b-bf16"))
    assert server.seen == []


def test_a_malformed_body_is_stored_and_raised(registry: ModelRegistry) -> None:
    store = RecordingStore()
    server = Server(b'{"object": "chat.completion", "choices": []}')
    backend = OpenAICompatibleBackend(
        registry.get("gemma-4-31b-it-bf16"), "http://gpu/v1", store, client=server.client()
    )
    with pytest.raises(BackendError, match="not a chat completion"):
        backend.complete(_request("gemma-4-31b-it-bf16"))
    assert b'{"object": "chat.completion", "choices": []}' in store.puts


def test_unknown_endpoint_recipes_fail_at_startup(registry: ModelRegistry) -> None:
    with pytest.raises(NotFoundError, match=r"llama-3\.3-70b"):
        backends_from_endpoints(
            (LlmEndpoint(recipe_id="llama-3.3-70b", base_url="http://gpu/v1"),),
            registry,
            MemoryStore(),
        )
