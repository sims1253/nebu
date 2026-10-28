"""Configuration from the environment. Only entry points (api, cli) read it."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class LlmEndpoint:
    """An OpenAI-compatible server (vLLM, Ollama, a hosted API) serving one model recipe.

    `api_key_env` names the environment variable that holds the server's API key; the backend
    reads it for each call. The key itself is never held in settings.
    """

    recipe_id: str
    base_url: str
    api_key_env: str | None = None


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _pairs(raw: str) -> list[tuple[str, str]]:
    """`key=value` pairs separated by commas."""
    out = []
    for item in filter(None, (part.strip() for part in raw.split(","))):
        key, _, value = item.partition("=")
        out.append((key.strip(), value.strip()))
    return out


def _endpoints(raw: str, keys: str = "") -> tuple[LlmEndpoint, ...]:
    """Parse `recipe=url` pairs, one per served model, and `recipe=ENV_VAR` key variables."""
    endpoints = _pairs(raw)
    served = {recipe_id for recipe_id, _ in endpoints}
    key_envs: dict[str, str] = {}
    for recipe_id, name in _pairs(keys):
        if recipe_id not in served:
            raise ValueError(
                f"KITTUM_LLM_API_KEYS names {recipe_id!r}, which no KITTUM_LLM_ENDPOINTS entry serves"
            )
        if not _ENV_NAME.fullmatch(name):
            # Not echoed: a key pasted here by mistake must not reach a log.
            raise ValueError(
                f"KITTUM_LLM_API_KEYS: the value for {recipe_id} must be the name of an"
                " environment variable that holds the key, not the key"
            )
        key_envs[recipe_id] = name
    return tuple(
        LlmEndpoint(recipe_id=recipe_id, base_url=url, api_key_env=key_envs.get(recipe_id))
        for recipe_id, url in endpoints
    )


@dataclass(frozen=True)
class Settings:
    database_url: str
    artifact_dir: Path
    contact_email: str | None = (
        None  # polite pools for Crossref, Unpaywall, NCBI; Unpaywall needs it
    )
    ncbi_api_key: str | None = None
    openalex_api_key: str | None = None
    llm_endpoints: tuple[LlmEndpoint, ...] = field(default=())
    # A deployment's own model recipes, in the format of kittum/models/recipes.yaml, added to the
    # shipped ones. Hosted APIs go here.
    recipes_file: Path | None = None
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
    # A directory of recorded source responses (`kittum.gateway.RecordedTransport`), allowed only
    # with `offline`. The gateway then answers from it, never touches the network, and caches in
    # memory, not in the deployment's cache: the end-to-end stack and offline demos.
    gateway_recordings: Path | None = None
    offline: bool = False
    # Record a live run to replay it later (`kittum.gateway.RecordingTransport`): every response
    # the gateway gets is written into this directory, in the format `gateway_recordings` reads,
    # without credentials or the contact address. Not with `offline`.
    gateway_record: Path | None = None
    # Record every model completion into this directory (or `.json` file), or replay from one
    # instead of calling the model servers (`kittum.models.RecordingBackend`, `ReplayBackend`).
    llm_record: Path | None = None
    llm_replay: Path | None = None

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
            llm_endpoints=_endpoints(
                env.get("KITTUM_LLM_ENDPOINTS", ""), env.get("KITTUM_LLM_API_KEYS", "")
            ),
            recipes_file=(
                Path(env["KITTUM_RECIPES_FILE"]) if env.get("KITTUM_RECIPES_FILE") else None
            ),
            grobid_url=env.get("KITTUM_GROBID_URL"),
            checkpoint_key_path=(
                Path(env["KITTUM_CHECKPOINT_KEY"]) if env.get("KITTUM_CHECKPOINT_KEY") else None
            ),
            judge_model=env.get("KITTUM_JUDGE_MODEL") or None,
            judge_backend=_judge_backend(env.get("KITTUM_JUDGE_BACKEND", "endpoint")),
            web_dist=Path(env["KITTUM_WEB_DIST"]) if env.get("KITTUM_WEB_DIST") else None,
            gateway_recordings=(
                Path(env["KITTUM_GATEWAY_RECORDINGS"])
                if env.get("KITTUM_GATEWAY_RECORDINGS")
                else None
            ),
            offline=_flag("KITTUM_OFFLINE", env.get("KITTUM_OFFLINE", "")),
            gateway_record=_path(env.get("KITTUM_GATEWAY_RECORD")),
            llm_record=_path(env.get("KITTUM_LLM_RECORD")),
            llm_replay=_path(env.get("KITTUM_LLM_REPLAY")),
        )


def _path(raw: str | None) -> Path | None:
    return Path(raw) if raw else None


def _flag(name: str, raw: str) -> bool:
    if raw.strip().lower() in ("", "0", "false", "no"):
        return False
    if raw.strip().lower() in ("1", "true", "yes"):
        return True
    raise ValueError(f"{name} must be 1 or 0, not {raw!r}")


def _judge_backend(raw: str) -> Literal["endpoint", "demo"]:
    if raw not in ("endpoint", "demo"):
        raise ValueError(f"KITTUM_JUDGE_BACKEND must be endpoint or demo, not {raw!r}")
    return raw
