"""Model recipes and backends (docs/document-tools.md, "Language models").

Every model call goes through an `LlmBackend`. A `ModelRecipe` pins what runs: repository,
revision, file hashes, and the weights licence. Kittum supports only licences on the allowlist
below, which carry no revenue caps, field-of-use limits, or no-competition clauses.

The gateway refuses to send a `local_only` document to a hosted backend.

Owner: WP-08 (with the judge). Shipped now: the types, the licence allowlist, and a recorded
backend in `kittum.models.testing`. Still to build: an OpenAI-compatible backend for vLLM and
Ollama with JSON Schema-constrained decoding, the recipe registry, and the confidentiality check.
"""

from enum import StrEnum
from typing import Any, Literal, Protocol

from kittum.core import Record, Sha256

ALLOWED_WEIGHTS_LICENCES = frozenset(
    {"apache-2.0", "mit", "bsd-3-clause", "openmdw-1.1", "public-domain"},
)


class Confidentiality(StrEnum):
    PUBLIC = "public"
    CONFIDENTIAL = "confidential"  # hosted EU models allowed with a DPA
    LOCAL_ONLY = "local_only"  # default for CSRs


class ModelRecipe(Record):
    id: str  # Kittum's name, e.g. "qwen3.8-27b-bf16"
    family: str  # for judge diversity, e.g. "qwen", "gemma", "nemotron"
    repository: str  # e.g. "Qwen/Qwen3.8-27B"
    revision: str
    file_hashes: tuple[tuple[str, Sha256], ...]
    weights_licence: str
    licence_url: str
    serving: Literal["vllm", "ollama", "hosted"]
    context_tokens: int
    vision: bool = False


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
    confidentiality: Confidentiality = Confidentiality.LOCAL_ONLY


class Completion(Record):
    text: str
    model_identity: str  # what the server reports, not the requested alias
    request_hash: Sha256
    raw_response_hash: Sha256


class LlmBackend(Protocol):
    def complete(self, request: CompletionRequest) -> Completion: ...


__all__ = [
    "ALLOWED_WEIGHTS_LICENCES",
    "Completion",
    "CompletionRequest",
    "Confidentiality",
    "LlmBackend",
    "Message",
    "ModelRecipe",
]
