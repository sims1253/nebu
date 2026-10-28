"""Search service functions: run live searches and persist executions."""

from typing import TYPE_CHECKING

from kittum.core import Clock, Id, SystemClock, new_id
from kittum.ledger import EventDraft, Ledger
from kittum.search.imports import _append_postgres_ledger
from kittum.search.store import save_execution

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.search.models import SearchExecution, SearchPlan, Source
    from kittum.storage import ArtifactStore


def run(
    tx: Tx,
    project_id: Id,
    plan: SearchPlan,
    source: Source,
    *,
    ledger: Ledger | None = None,
    actor_id: Id | None = None,
    artifacts: ArtifactStore | None = None,
    clock: Clock | None = None,
) -> SearchExecution:
    """Execute a live search plan via a source, assign occurrence IDs, and persist."""
    _ = artifacts
    execution = source.run(plan)

    # Ensure every record has an occurrence_id
    records_with_ids = [
        rec.model_copy(update={"occurrence_id": new_id()}) if not rec.occurrence_id else rec
        for rec in execution.records
    ]

    execution = execution.model_copy(update={"records": tuple(records_with_ids)})

    # Persist execution and source occurrences
    save_execution(tx, project_id, execution)

    # Append ledger event
    now = (clock or SystemClock()).now()
    if ledger is not None:
        draft = EventDraft(
            project_id=project_id,
            type="search.executed",
            schema_version=1,
            actor_id=actor_id or new_id(),
            payload={
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
            },
        )
        ledger.append(tx, draft)
    elif type(tx).__name__ != "NullTx":
        _append_postgres_ledger(tx, project_id, actor_id or new_id(), execution, now)

    return execution
