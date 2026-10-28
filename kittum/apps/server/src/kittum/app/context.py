"""The composition root: one `AppContext` per process, built from `Settings`.

`AppContext.from_settings` wires the Postgres ledger, the job queue and command receipts, the
artifact store, the source gateway (with the default policies and the httpx transport), the model
backends from `Settings.llm_endpoints`, and the screening judge. `job_handlers` gives a
`kittum.runs.Worker` the handler of every job kind a module registered: the screening judge and
extraction runs.
"""

import functools
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from kittum.core import Clock, KittumError, SystemClock
from kittum.db import Database
from kittum.extraction import JOB_KIND as EXTRACTION_JOB
from kittum.extraction import run_handler
from kittum.gateway import HttpxTransport, SourceGateway, default_policies
from kittum.judge import PROMPT_VERSION, JudgeConfig, judge_many
from kittum.ledger import PostgresLedger
from kittum.models import (
    Confidentiality,
    ModelRegistry,
    backends_from_endpoints,
)
from kittum.runs import Commands, PermanentJobError, PostgresJobQueue, PostgresReceipts
from kittum.screening import JUDGE_JOB, Screening, judge_handler
from kittum.storage import ArtifactStore, FilesystemStore

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from kittum.core import Id
    from kittum.db import Tx
    from kittum.gateway import Gateway
    from kittum.ledger import Ledger
    from kittum.models import LlmBackend, ModelRecipe, OpenAICompatibleBackend
    from kittum.runs import Handler, Job, JobContext, JobOutput, JobQueue, ReceiptStore
    from kittum.screening import JudgeRunner
    from kittum.settings import Settings

log = logging.getLogger(__name__)


class ConfigurationError(KittumError):
    """The settings cannot build a working process, for example a judge without a model server."""


@dataclass(frozen=True)
class Judge:
    """The screening judge this deployment runs: a config, its recipe, and the backend serving it.

    Screening records are `local_only` (the judge's default), so a hosted recipe is refused here,
    before any stage asks for it, as `check_confidentiality` would refuse each call.
    """

    config: JudgeConfig
    recipe: ModelRecipe
    backend: LlmBackend

    def __post_init__(self) -> None:
        if self.recipe.serving == "hosted":
            raise ConfigurationError(
                f"the judge recipe {self.recipe.id} is hosted; screening records are "
                f"{Confidentiality.LOCAL_ONLY.value} and may not leave this deployment"
            )

    @property
    def actor_ref(self) -> str:
        return f"model:{self.config.id}"

    def actor(self, tx: Tx) -> Id:
        """The model's actor row, created on first use."""
        return ensure_actor(tx, self.actor_ref, "model")

    def runner(self, *, actor_id: Id, ledger: Ledger, store: ArtifactStore, clock: Clock):
        """A `kittum.screening.JudgeRunner`. A laptop model server answers one call at a time."""
        runner: JudgeRunner = functools.partial(
            judge_many,
            actor_id=actor_id,
            config=self.config,
            recipe=self.recipe,
            backend=self.backend,
            ledger=ledger,
            store=store,
            clock=clock,
            concurrency=1 if self.recipe.serving == "ollama" else 4,
        )
        return runner


def ensure_actor(tx: Tx, identity_ref: str, kind: str) -> Id:
    row = tx.execute(
        "insert into actor (identity_ref, kind) values (%s, %s)"
        " on conflict (identity_ref) do update set kind = excluded.kind returning id",
        (identity_ref, kind),
    ).fetchone()
    assert row is not None
    return row["id"]


def judge_config(recipe_id: str, *, label: str | None = None) -> JudgeConfig:
    """The judge config for a recipe and the current prompt version."""
    return JudgeConfig(
        id=f"{label or recipe_id}@{PROMPT_VERSION}",
        model_id=recipe_id,
        prompt_version=PROMPT_VERSION,
    )


def build_judge(
    settings: Settings, registry: ModelRegistry, backends: Mapping[str, OpenAICompatibleBackend]
) -> Judge | None:
    """The configured judge, or None when no model is configured (the API still serves)."""
    if settings.judge_backend == "demo":
        from kittum.app.demo import demo_judge

        return demo_judge(registry)
    recipe_id = settings.judge_model or next(iter(backends), None)
    if recipe_id is None:
        return None
    backend = backends.get(recipe_id)
    if backend is None:
        raise ConfigurationError(
            f"KITTUM_JUDGE_MODEL is {recipe_id}, but no KITTUM_LLM_ENDPOINTS entry serves it"
        )
    return Judge(config=judge_config(recipe_id), recipe=backend.recipe, backend=backend)


def build_gateway(
    settings: Settings, db: Database, artifacts: ArtifactStore, clock: Clock
) -> SourceGateway:
    return SourceGateway(
        database=db,
        transport=HttpxTransport(contact_email=settings.contact_email),
        artifacts=artifacts,
        policies=default_policies(
            contact_email=settings.contact_email, ncbi_api_key=settings.ncbi_api_key
        ),
        clock=clock,
    )


@dataclass
class AppContext:
    """What routes, commands, and workers may use. Built once per process; tests build their own."""

    settings: Settings
    db: Database
    artifacts: ArtifactStore
    clock: Clock = field(default_factory=SystemClock)
    gateway: Gateway | None = None
    ledger: Ledger | None = None
    queue: JobQueue | None = None
    receipts: ReceiptStore | None = None
    backends: Mapping[str, LlmBackend] = field(default_factory=dict)
    """Every configured model server, by recipe ID (`Settings.llm_endpoints`)."""
    judge: Judge | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> AppContext:
        clock = SystemClock()
        db = Database(settings.database_url)
        artifacts = FilesystemStore(settings.artifact_dir)
        registry = ModelRegistry.load()
        backends = backends_from_endpoints(settings.llm_endpoints, registry, artifacts)
        return cls(
            settings=settings,
            db=db,
            artifacts=artifacts,
            clock=clock,
            gateway=build_gateway(settings, db, artifacts, clock),
            ledger=PostgresLedger(clock),
            queue=PostgresJobQueue(clock),
            receipts=PostgresReceipts(),
            backends=backends,
            judge=build_judge(settings, registry, backends),
        )

    def require_ledger(self) -> Ledger:
        if self.ledger is None:
            raise ConfigurationError("no ledger is configured")
        return self.ledger

    def screening(self) -> Screening:
        return Screening.postgres(self.require_ledger(), self.clock)


type HandlerFactory = Callable[[AppContext], Handler | None]
"""Builds a job kind's handler for a worker, or None when the context cannot run it."""

JOB_HANDLERS: dict[str, HandlerFactory] = {}


def register_job_handler(kind: str, factory: HandlerFactory) -> None:
    """Add a job kind to `kittum worker`. Extraction (WP-11) registers its jobs here."""
    if kind in JOB_HANDLERS and JOB_HANDLERS[kind] is not factory:
        raise ValueError(f"a different handler for {kind!r} is already registered")
    JOB_HANDLERS[kind] = factory


def _judge_jobs(ctx: AppContext) -> Handler | None:
    if ctx.judge is None:
        log.warning("no judge is configured, so this worker runs no %s jobs", JUDGE_JOB)
        return None
    with ctx.db.transaction() as tx:
        actor_id = ctx.judge.actor(tx)
    runner = ctx.judge.runner(
        actor_id=actor_id, ledger=ctx.require_ledger(), store=ctx.artifacts, clock=ctx.clock
    )
    screening = ctx.screening()
    inner = judge_handler(ctx.db, screening, runner, actor_id=actor_id)
    config_id = ctx.judge.config.id

    def handle(job: Job, job_ctx: JobContext) -> JobOutput:
        # A stage names its judge; refuse before any model call rather than judge with another.
        with ctx.db.transaction() as tx:
            info = screening.stage(tx, job.spec.project_id, UUID(job.spec.payload["stage_id"]))
        if info.judge_config_id != config_id:
            raise PermanentJobError(
                f"this worker runs judge {config_id}; the stage needs {info.judge_config_id}"
            )
        return inner(job, job_ctx)

    return handle


register_job_handler(JUDGE_JOB, _judge_jobs)


def _extraction_jobs(ctx: AppContext) -> Handler | None:
    if not ctx.backends or ctx.queue is None or ctx.receipts is None:
        log.warning("no model server or job queue is configured, so no %s jobs", EXTRACTION_JOB)
        return None
    commands = Commands(ctx.require_ledger(), ctx.queue, ctx.receipts, ctx.clock)
    return run_handler(
        db=ctx.db,
        artifacts=ctx.artifacts,
        commands=commands,
        backends=ctx.backends,
        clock=ctx.clock,
    )


register_job_handler(EXTRACTION_JOB, _extraction_jobs)


def job_handlers(ctx: AppContext) -> dict[str, Handler]:
    """Every registered job kind this context can run."""
    handlers = {kind: factory(ctx) for kind, factory in JOB_HANDLERS.items()}
    return {kind: handler for kind, handler in handlers.items() if handler is not None}


__all__ = [
    "JOB_HANDLERS",
    "AppContext",
    "ConfigurationError",
    "HandlerFactory",
    "Judge",
    "build_gateway",
    "build_judge",
    "ensure_actor",
    "job_handlers",
    "judge_config",
    "register_job_handler",
]
