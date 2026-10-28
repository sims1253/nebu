"""The `extraction.run` job: both paths over the run's layer, published with the job.

The handler reads its inputs in a short transaction, runs the paths outside any transaction
(the agent may take minutes), stores the proposal set and the agent transcript as artifacts, and
publishes the rows in the transaction that completes the job. A worker that lost its lease
publishes nothing, and a run that is already recorded is left alone.
"""

from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import UUID

from kittum.core import NotFoundError, canonical_json
from kittum.documents import get_document, get_layer
from kittum.extraction.extract import extract
from kittum.extraction.llm_path import PROMPT_VERSION
from kittum.extraction.store import record_proposals, run_inputs
from kittum.runs import JobOutput, PermanentJobError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kittum.core import Clock
    from kittum.models import LlmBackend
    from kittum.runs import Commands, Handler, Job, JobContext, Transactions
    from kittum.storage import ArtifactStore


def run_handler(
    *,
    db: Transactions,
    artifacts: ArtifactStore,
    commands: Commands,
    backends: Mapping[str, LlmBackend],
    clock: Clock,
) -> Handler:
    """The handler to register for `JOB_KIND`; `backends` are keyed by model recipe ID."""

    def handle(job: Job, _ctx: JobContext) -> JobOutput:
        run_id = UUID(job.spec.payload["run_id"])
        try:
            with db.transaction() as tx:
                run = run_inputs(tx, run_id)
                layer = get_layer(tx, run["layer_id"], artifacts=artifacts)
                document = get_document(tx, run["document_id"])
        except NotFoundError as e:
            raise PermanentJobError(str(e)) from e
        if run["status"] != "queued":
            return JobOutput()
        schema = run["schema"]
        agent = run["agent"]
        fields = schema.fields(run["study_id"], run["report_id"])
        result = extract(
            layer,
            document.artifact_hash,
            schema,
            fields,
            backend=backends.get(agent.model_id) if agent else None,
            agent=agent,
            clock=clock,
        )
        transcript_hash = None
        if result.transcript is not None:
            transcript_hash = artifacts.put(
                canonical_json({"prompt_version": PROMPT_VERSION, **asdict(result.transcript)})
            )
        output_hash = artifacts.put(
            canonical_json(
                {
                    "run_id": str(run_id),
                    "proposals": result.proposals.model_dump(mode="json"),
                    "reconciliations": [r.model_dump(mode="json") for r in result.reconciliations],
                }
            )
        )
        return JobOutput(
            output_hash=output_hash,
            publish=lambda tx: record_proposals(
                tx,
                commands,
                run_id,
                result,
                output_hash=output_hash,
                transcript_hash=transcript_hash,
                clock=clock,
            ),
        )

    return handle
