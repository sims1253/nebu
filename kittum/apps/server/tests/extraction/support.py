"""Schemas, agent settings, and recorded completions shared by the extraction tests."""

import json
from pathlib import Path

from kittum.extraction import AgentConfig, ExtractionSchema, load_schema
from kittum.models.testing import RecordedBackend
from tests.documents.conftest import LESOTHO, VITAMIN_C

FIXTURES = Path(__file__).parent / "fixtures"
RECIPE_ID = "qwen3.5-4b-ollama"
# Small budgets: the recordings come from a 4B model on a CPU, about a minute or two a call.
AGENT = AgentConfig(model_id=RECIPE_ID, max_calls=4)

DOCUMENTS = {"vitamin_c": VITAMIN_C, "lesotho": LESOTHO}


def schema(name: str) -> ExtractionSchema:
    return load_schema((FIXTURES / f"{name}_schema.yaml").read_text())


def recording_path(name: str) -> Path:
    return FIXTURES / f"ollama_qwen_{name}.json"


def recorded(name: str) -> RecordedBackend:
    """The completions the local Ollama gave for `name`, keyed by request hash."""
    data = json.loads(recording_path(name).read_text())
    return RecordedBackend(data["answers"], model_identity=data["model_identity"])
