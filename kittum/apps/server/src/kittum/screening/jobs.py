"""The judge job: run `kittum.judge` over a batch of a stage's items and attach the verdicts."""

import uuid
from typing import TYPE_CHECKING, Protocol

from kittum.runs import JobOutput, PermanentJobError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kittum.core import Id
    from kittum.db import Tx
    from kittum.judge import ScreeningItem, Verdict
    from kittum.protocol import Criterion
    from kittum.runs import Handler, Job, JobContext, Transactions
    from kittum.screening.service import Screening

_COMMANDS = uuid.UUID("0e7b6f43-58a4-4d55-9a8c-6a43d9c0f0c1")


class JudgeRunner(Protocol):
    """Judges items and records the verdicts, skipping what is already recorded.

    `functools.partial(kittum.judge.judge_many, actor_id=..., config=..., recipe=...,
    backend=..., ledger=..., store=..., clock=...)` is one: with `RecordedBackend` in tests and
    `OpenAICompatibleBackend` in production. The entry point builds it, because screening may
    not depend on `models` or `storage`.
    """

    def __call__(
        self,
        tx: Tx,
        items: Sequence[ScreeningItem],
        *,
        project_id: Id,
        scope_id: str,
        criteria: tuple[Criterion, ...],
        protocol_hash: str,
    ) -> list[Verdict]: ...


def judge_handler(
    db: Transactions, screening: Screening, runner: JudgeRunner, *, actor_id: Id
) -> Handler:
    """A `Worker` handler for `JUDGE_JOB`.

    The runner records its verdicts (and their raw outputs and events) in a transaction of its
    own, keyed by the judge's cache key, so a rerun after a crash makes no second model call.
    Attaching the verdicts to the stage is a command whose ID derives from the job, published
    with the job's completion; a worker that lost its lease attaches nothing.
    """

    def handle(job: Job, _ctx: JobContext) -> JobOutput:
        project_id = job.spec.project_id
        stage_id = uuid.UUID(job.spec.payload["stage_id"])
        with db.transaction() as tx:
            info, items, criteria = screening.judge_items(
                tx, project_id, stage_id, job.spec.payload["item_refs"]
            )
            verdicts = runner(
                tx,
                items,
                project_id=project_id,
                scope_id=info.scope_id,
                criteria=criteria,
                protocol_hash=info.protocol_hash,
            )
        wrong = {v.judge_config_id for v in verdicts} - {info.judge_config_id}
        if wrong:
            raise PermanentJobError(
                f"the runner judged with {sorted(wrong)}; the stage uses {info.judge_config_id}"
            )

        def publish(tx: Tx) -> None:
            screening.attach_verdicts(
                tx,
                command_id=uuid.uuid5(_COMMANDS, str(job.job_id)),
                project_id=project_id,
                actor_id=actor_id,
                stage_id=stage_id,
                verdicts=verdicts,
            )

        return JobOutput(publish=publish)

    return handle
