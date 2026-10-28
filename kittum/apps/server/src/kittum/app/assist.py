"""The protocol assistant in a deployment: which model it asks, and the steps of a request (who
may ask, the model calls outside any transaction, then what is stored). Hit counts go through
the source gateway, which an offline stack serves from recordings (`KITTUM_GATEWAY_RECORDINGS`).

`KITTUM_ASSIST_BACKEND=demo` replays the model answers recorded in `demo_data/assist`
(`tests/assist/record_fixtures.py` records them from a live model), for the end-to-end test and a
demo without a model server.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kittum import assist
from kittum.app.lifecycle import require_project_lead
from kittum.core import KittumError, canonical_json
from kittum.models import BackendError, Completion, CompletionRequest, request_key
from kittum.search import QueryCount, QueryDialect, count_hits

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kittum.app.context import AppContext
    from kittum.core import Id
    from kittum.models import ArtifactSink, LlmBackend
    from kittum.settings import Settings
    from kittum.storage import ArtifactStore

RECORDINGS = Path(__file__).parent / "demo_data" / "assist"


class AssistUnavailable(KittumError):
    """No model is configured for the assistant, or nothing to count hits with."""


@dataclass(frozen=True)
class Assistant:
    """The model the protocol assistant asks."""

    recipe_id: str
    backend: LlmBackend


class ReplayBackend:
    """Answers a request with the answer recorded for it, keyed by `request_key` as
    `kittum.models.testing.RecordedBackend` keys them; a request with no recording fails as a
    model server would. The request and the answer go to the artifact store like live bytes."""

    def __init__(
        self, answers: Mapping[str, str], model_identity: str, store: ArtifactSink
    ) -> None:
        self._answers = answers
        self._store = store
        self.model_identity = model_identity

    def complete(self, request: CompletionRequest) -> Completion:
        key = request_key(request)
        raw_request_hash = self._store.put(canonical_json(request))
        text = self._answers.get(key)
        if text is None:
            raise BackendError(f"no recorded answer for request {key}")
        return Completion(
            text=text,
            model_identity=self.model_identity,
            request_hash=key,
            raw_request_hash=raw_request_hash,
            raw_response_hash=self._store.put(text.encode()),
        )


def recorded_backend(
    store: ArtifactSink, path: Path = RECORDINGS / "answers.json"
) -> tuple[str, ReplayBackend]:
    """The recorded model answers and the recipe they were recorded with."""
    data = json.loads(path.read_text())
    identity = f"recorded:{data['model_identity']}"
    return data["recipe_id"], ReplayBackend(data["answers"], identity, store)


def build_assistant(
    settings: Settings, backends: Mapping[str, LlmBackend], artifacts: ArtifactStore
) -> Assistant | None:
    """The configured assistant, or None when no model is configured."""
    if settings.assist_backend == "demo":
        recipe_id, backend = recorded_backend(artifacts)
        return Assistant(recipe_id=recipe_id, backend=backend)
    recipe_id = settings.assist_model or settings.judge_model or next(iter(backends), None)
    if recipe_id is None:
        return None
    backend = backends.get(recipe_id)
    if backend is None:
        from kittum.app.context import ConfigurationError

        raise ConfigurationError(
            f"KITTUM_ASSIST_MODEL is {recipe_id}, but no KITTUM_LLM_ENDPOINTS entry serves it"
        )
    return Assistant(recipe_id=recipe_id, backend=backend)


def _assistant(ctx: AppContext) -> Assistant:
    if ctx.assistant is None:
        raise AssistUnavailable(
            "no model is configured for the protocol assistant (KITTUM_ASSIST_MODEL)"
        )
    return ctx.assistant


def _lead(ctx: AppContext, project_id: Id, actor_id: Id) -> None:
    with ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, actor_id)


def draft_protocol(
    ctx: AppContext, project_id: Id, actor_id: Id, description: str, language: str = "en"
) -> assist.AssistOutcome:
    """A first protocol from the lead's description, as a new draft revision."""
    assistant = _assistant(ctx)
    _lead(ctx, project_id, actor_id)
    answer = assist.draft(assistant.backend, assistant.recipe_id, description, language)
    with ctx.db.transaction() as tx:
        return assist.record(tx, project_id, actor_id, answer, ledger=ctx.require_ledger())


def improve_part(
    ctx: AppContext, project_id: Id, actor_id: Id, revision_id: Id, part: str
) -> assist.AssistOutcome:
    """The model's changes to one part of a revision, as a suggestion revision."""
    assistant = _assistant(ctx)
    with ctx.db.transaction() as tx:
        require_project_lead(tx, project_id, actor_id)
        base = assist.get_revision(tx, project_id, revision_id)
    if assist.part_kind(part) is None:
        raise assist.AssistError(
            f"{part!r} is not a part the assistant improves: a question, a criterion, or a"
            " source's strategy"
        )
    try:
        answer = assist.improve(
            assistant.backend, assistant.recipe_id, base.id, base.document, part
        )
    except KeyError, StopIteration:
        raise assist.AssistError(f"revision {base.seq} has no {part}") from None
    with ctx.db.transaction() as tx:
        return assist.record(tx, project_id, actor_id, answer, ledger=ctx.require_ledger())


def count_query(
    ctx: AppContext, project_id: Id, actor_id: Id, dialect: QueryDialect, strategy: str
) -> QueryCount:
    """How many records a strategy finds today. Informational: no search runs."""
    gateway = ctx.gateway
    if gateway is None:
        raise AssistUnavailable("no gateway is configured to count hits with")
    _lead(ctx, project_id, actor_id)
    return count_hits(
        gateway,
        ctx.artifacts,
        dialect,
        strategy,
        contact_email=ctx.settings.contact_email,
        ncbi_api_key=ctx.settings.ncbi_api_key,
        clock=ctx.clock,
    )
