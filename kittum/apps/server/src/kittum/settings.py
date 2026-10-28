"""Configuration from the environment. Only entry points (api, cli) read it."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class LlmEndpoint:
    """An OpenAI-compatible server (vLLM, Ollama) serving one model recipe."""

    recipe_id: str
    base_url: str
    api_key: str = "unused"


def _endpoints(raw: str) -> tuple[LlmEndpoint, ...]:
    """Parse `recipe=url` pairs separated by commas, one per served model."""
    out = []
    for item in filter(None, (part.strip() for part in raw.split(","))):
        recipe_id, _, url = item.partition("=")
        out.append(LlmEndpoint(recipe_id=recipe_id.strip(), base_url=url.strip()))
    return tuple(out)


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_dir: Path
    contact_email: str | None = None  # polite pools for Crossref, Unpaywall, NCBI
    ncbi_api_key: str | None = None
    openalex_api_key: str | None = None
    llm_endpoints: tuple[LlmEndpoint, ...] = field(default=())
    grobid_url: str | None = None
    # Ed25519 private key (PEM) that signs ledger checkpoints and bundles. Hold it apart from the
    # database administrator: `openssl genpkey -algorithm ed25519 -out checkpoint.pem`.
    checkpoint_key_path: Path | None = None
    # The screening judge: a recipe ID from `kittum.models.ModelRegistry`, served by one of
    # `llm_endpoints`. Defaults to the first endpoint's recipe.
    judge_model: str | None = None
    # `endpoint` calls the model server; `demo` replays the demo project's recorded answers, for
    # the end-to-end test and a demo without a model server.
    judge_backend: Literal["endpoint", "demo"] = "endpoint"
    # The built workbench (`pnpm build` writes apps/web/dist). `kittum serve` serves it when set.
    web_dist: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            database_url=env.get(
                "KITTUM_DATABASE_URL", "postgresql://kittum:kittum@127.0.0.1:55432/kittum"
            ),
            artifact_dir=Path(env.get("KITTUM_ARTIFACT_DIR", ".kittum/artifacts")),
            contact_email=env.get("KITTUM_CONTACT_EMAIL"),
            ncbi_api_key=env.get("KITTUM_NCBI_API_KEY"),
            openalex_api_key=env.get("KITTUM_OPENALEX_API_KEY"),
            llm_endpoints=_endpoints(env.get("KITTUM_LLM_ENDPOINTS", "")),
            grobid_url=env.get("KITTUM_GROBID_URL"),
            checkpoint_key_path=(
                Path(env["KITTUM_CHECKPOINT_KEY"]) if env.get("KITTUM_CHECKPOINT_KEY") else None
            ),
            judge_model=env.get("KITTUM_JUDGE_MODEL") or None,
            judge_backend=_judge_backend(env.get("KITTUM_JUDGE_BACKEND", "endpoint")),
            web_dist=Path(env["KITTUM_WEB_DIST"]) if env.get("KITTUM_WEB_DIST") else None,
        )


def _judge_backend(raw: str) -> Literal["endpoint", "demo"]:
    if raw not in ("endpoint", "demo"):
        raise ValueError(f"KITTUM_JUDGE_BACKEND must be endpoint or demo, not {raw!r}")
    return raw
