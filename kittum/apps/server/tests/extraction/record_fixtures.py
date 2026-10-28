"""Record the model completions that the LLM-path tests replay.

Run once, by hand, with the local Ollama serving `qwen3.5:4b`:

    uv run python -m tests.extraction.record_fixtures [vitamin_c] [lesotho]

This is not a test. It parses the fixture PDFs, runs the agent against Ollama with the same
schemas and budget as the tests, and stores every completion by request hash.
"""

import json
import sys
import uuid

from kittum.core import SystemClock
from kittum.documents import Layer, PyMuPDFParser
from kittum.extraction import run_agent
from kittum.models import ModelRegistry, OpenAICompatibleBackend
from kittum.storage import MemoryStore
from tests.extraction.support import AGENT, DOCUMENTS, RECIPE_ID, recording_path, schema

OLLAMA = "http://127.0.0.1:11434/v1"


class Recorder:
    def __init__(self, inner: OpenAICompatibleBackend) -> None:
        self.inner = inner
        self.answers: dict[str, str] = {}
        self.identity = ""

    def complete(self, request):
        completion = self.inner.complete(request)
        self.answers[completion.request_hash] = completion.text
        self.identity = completion.model_identity
        print(f"  call {len(self.answers)}: {completion.text[:200]}", flush=True)
        return completion


def record(name: str) -> None:
    parser = PyMuPDFParser()
    raw = parser.parse(DOCUMENTS[name].read_bytes())
    layer = Layer(
        id=uuid.uuid7(),
        document_id=uuid.uuid7(),
        recipe=parser.recipe,
        recipe_hash="0" * 64,
        output_hash="0" * 64,
        **raw.model_dump(),
    )
    fields = schema(name).fields(uuid.uuid7(), uuid.uuid7())
    recipe = ModelRegistry.load().get(RECIPE_ID)
    recorder = Recorder(OpenAICompatibleBackend(recipe, OLLAMA, MemoryStore(), timeout=1200))
    print(f"{name}: {len(fields)} fields", flush=True)
    proposals, _, transcript = run_agent(layer, fields, recorder, AGENT, SystemClock())
    for p in proposals:
        print(
            f"  {p.key.endpoint}/{p.key.arm}/{p.key.quantity}: {p.status} {p.value} {p.message or ''}"
        )
    print(f"  tools: {transcript.tools} stopped: {transcript.stopped}")
    recording_path(name).write_text(
        json.dumps(
            {
                "recipe_id": RECIPE_ID,
                "model_identity": recorder.identity,
                "answers": recorder.answers,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


if __name__ == "__main__":
    for name in sys.argv[1:] or list(DOCUMENTS):
        record(name)
