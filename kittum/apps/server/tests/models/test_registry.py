"""The shipped recipes, a deployment's own recipes, and the weights licence allowlist."""

import re
from typing import TYPE_CHECKING

import pytest

from kittum.models import ALLOWED_WEIGHTS_LICENCES, ModelRegistry, RecipeError, weights_hash

if TYPE_CHECKING:
    from pathlib import Path

GOOD = """
recipes:
  - id: gemma-4-e4b-it-ollama
    family: gemma
    repository: registry.ollama.ai/library/gemma4
    served_name: "gemma4:e4b"
    revision: "sha256:dc35e8d9c6061baa6f0fa870975ab6932e2542b579b13ea0f199fa4bb7300c9c"
    weights_licence: Apache-2.0
    licence_url: https://ai.google.dev/gemma/docs/gemma_4_license
    serving: ollama
    context_tokens: 131072
    file_hashes:
      - [model, 370c2879f17648d337b0ab4e208fdce348e24d1060064e93d7645daafd56d39c]
"""


def test_shipped_recipes_cover_the_picks_with_allowed_licences() -> None:
    registry = ModelRegistry.load()
    by_id = {r.id: r for r in registry}
    assert {
        "qwen3.8-27b-bf16",
        "gemma-4-31b-it-bf16",
        "nemotron-3.5-super-vl-nvfp4",
        "gemma-4-e4b-it-ollama",
        "gemma-4-12b-it-ollama",
        "qwen3.5-4b-ollama",
    } <= by_id.keys()
    assert {r.family for r in registry} >= {"qwen", "gemma", "nemotron"}
    assert all(r.weights_licence in ALLOWED_WEIGHTS_LICENCES for r in registry)
    assert by_id["qwen3.8-27b-bf16"].repository == "Qwen/Qwen3.8-27B"
    assert by_id["gemma-4-31b-it-bf16"].repository == "google/gemma-4-31B-it"
    assert by_id["nemotron-3.5-super-vl-nvfp4"].weights_licence == "openmdw-1.1"
    assert len(by_id["qwen3.8-27b-bf16"].file_hashes) == 18
    assert len(by_id["nemotron-3.5-super-vl-nvfp4"].file_hashes) == 88
    # Thinking on or off is a decoding choice: the weights, and so their hash, are the same.
    assert weights_hash(by_id["qwen3.8-27b-bf16"]) == weights_hash(
        by_id["qwen3.8-27b-bf16-thinking"]
    )
    assert weights_hash(by_id["qwen3.8-27b-bf16"]) != weights_hash(by_id["gemma-4-31b-it-bf16"])


@pytest.mark.parametrize(
    ("licence", "allowed"),
    [
        ("Apache-2.0", True),
        ("openmdw-1.1", True),
        ("llama3.3", False),
        ("cc-by-nc-4.0", False),
        ("lfm1.0", False),
        ("health-ai-developer-foundations", False),
    ],
)
def test_recipes_outside_the_allowlist_are_rejected(licence: str, allowed: bool) -> None:
    text = GOOD.replace("Apache-2.0", licence)
    if allowed:
        assert len(ModelRegistry.from_yaml(text)) == 1
    else:
        with pytest.raises(RecipeError, match=r"gemma-4-e4b-it-ollama: weights licence"):
            ModelRegistry.from_yaml(text)


def test_malformed_and_duplicate_recipes_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RecipeError, match="gemma-4-e4b-it-ollama"):
        ModelRegistry.from_yaml(GOOD.replace("serving: ollama", "serving: llamafile"))
    with pytest.raises(RecipeError, match="defined twice"):
        ModelRegistry.from_yaml(GOOD + GOOD.removeprefix("\nrecipes:\n"))
    with pytest.raises(RecipeError, match="pins no weight files"):
        ModelRegistry.from_yaml(GOOD.split("    file_hashes:")[0] + "    file_hashes: []\n")
    path = tmp_path / "recipes.yaml"
    path.write_text(GOOD)
    assert ModelRegistry.load(path).get("gemma-4-e4b-it-ollama").api_model == "gemma4:e4b"


HOSTED = """
recipes:
  - id: example-hosted
    family: example
    repository: example-org/example-model
    served_name: example-model
    revision: "2026-10"
    weights_licence: mit
    licence_url: https://example.org/LICENSE
    serving: hosted
    context_tokens: 131072
    extra_body: {"thinking": {"type": "disabled"}}
"""


def test_a_deployments_own_recipes_join_the_shipped_ones(tmp_path: Path) -> None:
    """A hosted API pins no weight files, but its licence is checked like any other, and it may
    not take the ID of a shipped recipe."""
    local = tmp_path / "recipes.yaml"
    local.write_text(HOSTED)
    registry = ModelRegistry.load(local=local)
    assert len(registry) == len(ModelRegistry.load()) + 1
    hosted = registry.get("example-hosted")
    assert (hosted.serving, hosted.api_model) == ("hosted", "example-model")
    assert hosted.extra_body == {"thinking": {"type": "disabled"}}
    assert registry.get("qwen3.5-4b-ollama").serving == "ollama"

    local.write_text(HOSTED.replace("mit", "cc-by-nc-4.0"))
    with pytest.raises(RecipeError, match=r"example-hosted: weights licence 'cc-by-nc-4\.0'"):
        ModelRegistry.load(local=local)
    local.write_text(HOSTED.replace("example-hosted", "qwen3.5-4b-ollama"))
    with pytest.raises(RecipeError, match=r"qwen3\.5-4b-ollama has the ID of a built-in recipe"):
        ModelRegistry.load(local=local)
    local.write_text(HOSTED.replace("serving: hosted", "serving: vllm"))
    with pytest.raises(RecipeError, match="example-hosted pins no weight files"):
        ModelRegistry.load(local=local)
    local.write_text(HOSTED.replace("serving: hosted", "serving: openai"))
    with pytest.raises(RecipeError, match=re.escape(f"{local}: recipe example-hosted")):
        ModelRegistry.load(local=local)
    with pytest.raises(RecipeError, match="cannot read the recipes file"):
        ModelRegistry.load(local=tmp_path / "missing.yaml")
