"""Configuration from the environment. Only entry points (api, cli) read it."""

import os
from dataclasses import dataclass, field
from pathlib import Path


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
        )
