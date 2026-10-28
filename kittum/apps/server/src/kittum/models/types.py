"""The values that cross the model seam."""

from typing import Any, Literal, Protocol

from pydantic import Field

from kittum.core import KittumError, Record, Sha256, content_hash

ALLOWED_WEIGHTS_LICENCES = frozenset(
    {"apache-2.0", "mit", "bsd-3-clause", "openmdw-1.1", "public-domain"},
)


class ModelRecipe(Record):
    id: str  # Kittum's name, e.g. "qwen3.8-27b-bf16"
    family: str  # for judge diversity, e.g. "qwen", "gemma", "nemotron"
    repository: str  # e.g. "Qwen/Qwen3.8-27B"
    revision: str
    file_hashes: tuple[tuple[str, Sha256], ...] = ()  # required unless `serving` is hosted
    weights_licence: str
    licence_url: str
    serving: Literal["vllm", "ollama", "hosted"]
    context_tokens: int
    vision: bool = False
    served_name: str | None = None  # the name the server answers to; defaults to `repository`
    upstream_repository: str | None = None  # where the weights and their licence come from
    reasoning: bool = False  # the model may think before it answers
    extra_body: dict[str, Any] = Field(default_factory=dict)  # merged into every request body
    notes: str = ""

    @property
    def api_model(self) -> str:
        return self.served_name or self.repository


def weights_hash(recipe: ModelRecipe) -> str:
    """Identifies the weights a recipe runs, independent of its name and serving options."""
    return content_hash(
        {
            "repository": recipe.repository,
            "revision": recipe.revision,
            "file_hashes": [list(pair) for pair in recipe.file_hashes],
        },
        domain="kittum.model_weights.v1",
    )


class Message(Record):
    role: Literal["system", "user", "assistant"]
    content: str


class CompletionRequest(Record):
    model_id: str
    messages: tuple[Message, ...]
    json_schema: dict[str, Any] | None = None
    temperature: float = 0.0
    seed: int | None = 0
    max_tokens: int = 2048


def request_key(request: CompletionRequest) -> str:
    """The identity of a request, used as `Completion.request_hash` by every backend."""
    return content_hash(request, domain="kittum.completion_request.v1")


class Completion(Record):
    text: str  # the answer, with any thinking removed
    model_identity: str  # what the server reports, not the requested alias
    request_hash: Sha256
    raw_response_hash: Sha256
    raw_request_hash: Sha256 | None = None  # the exact bytes sent, in the artifact store
    reasoning: str | None = None
    finish_reason: str | None = None
    # Where the request went, for the AI disclosure: the recipe's serving and the server's base URL
    # without credentials. None when nothing was sent, as in a replay.
    serving: str | None = None
    endpoint: str | None = None  # "replay of <endpoint>" when replayed (kittum.models.replay)


class LlmBackend(Protocol):
    def complete(self, request: CompletionRequest) -> Completion: ...


class RecipeBackend(LlmBackend, Protocol):
    """A backend that serves one recipe, as a configured endpoint or its replay does."""

    @property
    def recipe(self) -> ModelRecipe: ...


class BackendError(KittumError):
    """The model server could not be reached, or answered without a usable completion.

    `raw_request_hash` and `raw_response_hash` name the bytes already in the artifact store, so a
    failed call stays on record."""

    def __init__(
        self,
        message: str,
        *,
        raw_request_hash: str | None = None,
        raw_response_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_request_hash = raw_request_hash
        self.raw_response_hash = raw_response_hash
