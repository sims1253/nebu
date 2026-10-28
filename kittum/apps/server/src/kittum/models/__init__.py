"""Model recipes and backends (docs/document-tools.md, "Language models").

Every model call goes through an `LlmBackend`. A `ModelRecipe` pins what runs: repository,
revision, file hashes, and the weights licence. Kittum supports only licences on the allowlist,
which carry no revenue caps, field-of-use limits, or no-competition clauses; `ModelRegistry`
refuses to load any other recipe. The shipped recipes live in `recipes.yaml`.

`OpenAICompatibleBackend` talks to vLLM, Ollama, or a hosted API with JSON Schema-constrained
decoding, stores the raw request and response bytes, and reports the model identity the server
returns, the recipe's serving, and the endpoint. `backends_from_endpoints` builds one per entry in
`Settings.llm_endpoints`. Which content goes to which server is the deployment's choice; a hosted
API's key comes from an environment variable and is never stored. A deployment adds its own
recipes, hosted ones included, with `ModelRegistry.load(local=...)`.

Owner: WP-08. `kittum.models.testing.RecordedBackend` replays stored answers for tests.
"""

from kittum.models.openai_compat import (
    ArtifactSink,
    Endpoint,
    OpenAICompatibleBackend,
    backends_from_endpoints,
)
from kittum.models.reasoning import split_reasoning
from kittum.models.registry import ModelRegistry, RecipeError
from kittum.models.types import (
    ALLOWED_WEIGHTS_LICENCES,
    BackendError,
    Completion,
    CompletionRequest,
    LlmBackend,
    Message,
    ModelRecipe,
    request_key,
    weights_hash,
)

__all__ = [
    "ALLOWED_WEIGHTS_LICENCES",
    "ArtifactSink",
    "BackendError",
    "Completion",
    "CompletionRequest",
    "Endpoint",
    "LlmBackend",
    "Message",
    "ModelRecipe",
    "ModelRegistry",
    "OpenAICompatibleBackend",
    "RecipeError",
    "backends_from_endpoints",
    "request_key",
    "split_reasoning",
    "weights_hash",
]
