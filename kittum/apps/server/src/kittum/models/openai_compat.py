"""An `LlmBackend` for OpenAI-compatible servers: vLLM, Ollama, and hosted EU APIs.

Every call asks for JSON Schema-constrained output through `response_format`:

    {"type": "json_schema", "json_schema": {"name": ..., "schema": ..., "strict": true}}

vLLM compiles the schema with its structured-output backend (xgrammar, or llguidance as a
fallback) and, when started with `--reasoning-parser`, applies it only after the thinking ends,
returning the thinking in `message.reasoning` (`reasoning_content` before vLLM 0.11). Ollama turns
the same field into its `format` grammar and returns thinking in `message.reasoning`. Both read
`seed`; Ollama maps `max_tokens` to `num_predict`. Model-specific switches, such as
`chat_template_kwargs.enable_thinking` for vLLM or `reasoning_effort` for Ollama, come from the
recipe's `extra_body`.

The exact request and response bytes go to the artifact store before anything else happens, so a
failed or odd answer is still on record. Only transport errors are retried; an HTTP error status
or an unusable body raises `BackendError` at once.
"""

import json
from typing import TYPE_CHECKING, Any, Protocol

import httpx2
import stamina

from kittum.core import NotFoundError, canonical_json
from kittum.models.reasoning import split_reasoning
from kittum.models.types import (
    BackendError,
    Completion,
    CompletionRequest,
    ModelRecipe,
    check_confidentiality,
    request_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kittum.models.registry import ModelRegistry

SCHEMA_NAME = "kittum_answer"


class ArtifactSink(Protocol):
    """The part of `kittum.storage.ArtifactStore` a backend needs."""

    def put(self, data: bytes) -> str: ...


class Endpoint(Protocol):
    """The shape of `kittum.settings.LlmEndpoint`."""

    @property
    def recipe_id(self) -> str: ...

    @property
    def base_url(self) -> str: ...

    @property
    def api_key(self) -> str: ...


class OpenAICompatibleBackend:
    """Serves one recipe from one server. `base_url` ends in `/v1`."""

    def __init__(
        self,
        recipe: ModelRecipe,
        base_url: str,
        store: ArtifactSink,
        *,
        api_key: str = "unused",
        client: httpx2.Client | None = None,
        timeout: float = 600.0,
        attempts: int = 3,
        wait_initial: float = 1.0,
    ) -> None:
        self.recipe = recipe
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._store = store
        self._headers = {"authorization": f"Bearer {api_key}", "content-type": "application/json"}
        self._client = client or httpx2.Client(timeout=timeout)
        self._attempts = attempts
        self._wait_initial = wait_initial

    def body(self, request: CompletionRequest) -> dict[str, Any]:
        """The JSON body sent for `request`."""
        body: dict[str, Any] = {
            "model": self.recipe.api_model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "schema": request.json_schema, "strict": True},
            }
        body.update(self.recipe.extra_body)
        return body

    def complete(self, request: CompletionRequest) -> Completion:
        check_confidentiality(self.recipe, request)
        if request.model_id != self.recipe.id:
            raise ValueError(f"backend serves {self.recipe.id}, not {request.model_id}")
        raw_request = canonical_json(self.body(request))
        raw_request_hash = self._store.put(raw_request)
        response = self._post(raw_request)
        raw_response_hash = self._store.put(response.content)
        if response.status_code >= 400:
            raise BackendError(
                f"{self.recipe.id}: HTTP {response.status_code}: {response.text[:500]}"
            )
        try:
            data = json.loads(response.content)
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise BackendError(f"{self.recipe.id}: not a chat completion: {e}") from e
        thinking, answer = split_reasoning(message.get("content") or "")
        reasoning = message.get("reasoning") or message.get("reasoning_content") or thinking
        return Completion(
            text=answer,
            model_identity=str(data.get("model") or f"unreported:{self.recipe.api_model}"),
            request_hash=request_key(request),
            raw_response_hash=raw_response_hash,
            raw_request_hash=raw_request_hash,
            reasoning=reasoning,
            finish_reason=choice.get("finish_reason"),
        )

    def _post(self, raw_request: bytes) -> httpx2.Response:
        for attempt in stamina.retry_context(
            on=httpx2.TransportError,
            attempts=self._attempts,
            timeout=None,
            wait_initial=self._wait_initial,
            wait_jitter=self._wait_initial,
        ):
            with attempt:
                return self._client.post(self._url, content=raw_request, headers=self._headers)
        raise AssertionError("unreachable")


def backends_from_endpoints(
    endpoints: Iterable[Endpoint],
    registry: ModelRegistry,
    store: ArtifactSink,
    *,
    client: httpx2.Client | None = None,
) -> dict[str, OpenAICompatibleBackend]:
    """One backend per configured endpoint (`Settings.llm_endpoints`), keyed by recipe ID."""
    backends: dict[str, OpenAICompatibleBackend] = {}
    for endpoint in endpoints:
        try:
            recipe = registry.get(endpoint.recipe_id)
        except NotFoundError:
            raise NotFoundError(
                f"endpoint {endpoint.base_url} names unknown recipe {endpoint.recipe_id!r}"
            ) from None
        backends[recipe.id] = OpenAICompatibleBackend(
            recipe, endpoint.base_url, store, api_key=endpoint.api_key, client=client
        )
    return backends
