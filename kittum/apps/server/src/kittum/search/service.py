"""Search service functions: run live searches and persist executions."""

from typing import TYPE_CHECKING, Any

from kittum.core import Id, new_id
from kittum.ledger import EventDraft
from kittum.search.store import save_execution

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import Ledger
    from kittum.search.models import SearchExecution, SearchPlan, Source
    from kittum.storage import ArtifactStore


def run(
    tx: Tx,
    project_id: Id,
    plan: SearchPlan,
    source: Source,
    *,
    ledger: Ledger,
    actor_id: Id,
    artifacts: ArtifactStore,
) -> SearchExecution:
    """Execute a live search plan via a source, then persist it with its ledger event.

    The source stores its raw responses; every hash the execution references must already be in
    `artifacts`, or nothing is written.
    """
    execution = source.run(plan)
    if execution.plan != plan:
        raise ValueError(f"source {source.name!r} returned an execution for a different plan")
    return record_execution(
        tx, project_id, execution, ledger=ledger, actor_id=actor_id, artifacts=artifacts
    )


def record_execution(
    tx: Tx,
    project_id: Id,
    execution: SearchExecution,
    *,
    ledger: Ledger,
    actor_id: Id,
    artifacts: ArtifactStore,
) -> SearchExecution:
    """Assign occurrence IDs, check counts and artifacts, and write rows and `search.executed`."""
    if execution.retrieved_count != len(execution.records):
        raise ValueError(
            f"retrieved_count is {execution.retrieved_count} "
            f"but the execution has {len(execution.records)} records"
        )
    referenced = set(execution.raw_artifact_hashes)
    stray = {r.raw_hash for r in execution.records} - referenced
    if stray:
        raise ValueError(f"records reference raw hashes not in the execution: {sorted(stray)}")
    missing = [h for h in execution.raw_artifact_hashes if not artifacts.exists(h)]
    if missing:
        raise ValueError(f"raw responses are not in the artifact store: {missing}")

    # Occurrence IDs belong to the search module, whatever the source filled in.
    execution = execution.model_copy(
        update={
            "records": tuple(
                r.model_copy(update={"occurrence_id": new_id()}) for r in execution.records
            )
        }
    )
    save_execution(tx, project_id, execution)
    ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type="search.executed",
            schema_version=1,
            actor_id=actor_id,
            payload=prisma_s_payload(execution),
        ),
    )
    return execution


def prisma_s_payload(execution: SearchExecution) -> dict[str, Any]:
    """What PRISMA-S asks to report about one search, as the `search.executed` payload."""
    plan = execution.plan
    return {
        "execution_id": str(execution.id),
        "topic_id": plan.topic_id,
        "source": plan.source,
        "platform": plan.platform,
        "kind": execution.kind,
        "strategy": plan.strategy,
        "limits": list(plan.limits),
        "query_translation": execution.query_translation,
        "reported_count": execution.reported_count,
        "retrieved_count": execution.retrieved_count,
        "started_at": execution.started_at.isoformat(),
        "ended_at": execution.ended_at.isoformat(),
        "raw_artifact_hashes": list(execution.raw_artifact_hashes),
        "skipped_record_ids": list(execution.skipped_record_ids),
    }
