from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id, sha256_hex
from kittum.ledger.testing import InMemoryLedger
from kittum.search import SearchExecution, SearchPlan, SourceRecord, get_execution, run
from kittum.search.testing import FakeSource
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    import uuid

    from kittum.db import Tx

pytestmark = pytest.mark.postgres

PLAN = SearchPlan(topic_id="t1", source="fake", platform="TestPlatform", strategy="my search")
PLAN_TIME = datetime(2026, 10, 28, tzinfo=UTC)


def _record(source_occurrence_id: uuid.UUID, raw_hash: str) -> SourceRecord:
    return SourceRecord(
        occurrence_id=source_occurrence_id,
        source="fake",
        provider_record_id="rec-1",
        ordinal=0,
        title="Test Title",
        raw_hash=raw_hash,
    )


def test_search_assigns_occurrence_ids_and_keeps_the_sources_counts(
    pg: Tx, project_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    from_source = new_id()
    artifacts = MemoryStore()
    fake = FakeSource(
        artifacts,
        records=(_record(from_source, sha256_hex(b"ignored")),),
        reported_count=40,
        query_translation="translated",
    )
    ledger = InMemoryLedger()

    execution = run(
        pg, project_id, PLAN, fake, ledger=ledger, actor_id=actor_id, artifacts=artifacts
    )

    # The source's occurrence ID is replaced; the stored row carries the search module's.
    (record,) = execution.records
    assert record.occurrence_id != from_source
    loaded = get_execution(pg, project_id, execution.id)
    assert loaded is not None
    assert loaded.records[0].occurrence_id == record.occurrence_id

    # Reported is what the source said; retrieved is what was stored.
    assert loaded.reported_count == 40
    assert loaded.retrieved_count == 1
    assert loaded.query_translation == "translated"
    assert fake.plans == [PLAN]

    (event,) = ledger.events(pg, project_id)
    assert event.type == "search.executed"
    assert event.payload["reported_count"] == 40
    assert event.payload["retrieved_count"] == 1


def _canned(raw_hash: str, *, retrieved_count: int = 1) -> SearchExecution:
    return SearchExecution(
        id=new_id(),
        plan=PLAN,
        kind="live_api",
        started_at=PLAN_TIME,
        ended_at=PLAN_TIME,
        query_translation=None,
        reported_count=1,
        retrieved_count=retrieved_count,
        raw_artifact_hashes=(raw_hash,),
        records=(_record(new_id(), raw_hash),),
    )


@pytest.mark.parametrize(
    ("stored", "retrieved_count", "error"),
    [
        (False, 1, "not in the artifact store"),
        (True, 2, "retrieved_count is 2"),
    ],
)
def test_search_writes_nothing_when_an_execution_is_inconsistent(
    pg: Tx,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    stored: bool,
    retrieved_count: int,
    error: str,
) -> None:
    artifacts = MemoryStore()
    body = b"a raw response"
    if stored:
        artifacts.put(body)
    canned = _canned(sha256_hex(body), retrieved_count=retrieved_count)
    fake = FakeSource(artifacts, executions={PLAN.strategy: canned})
    ledger = InMemoryLedger()

    with pytest.raises(ValueError, match=error):
        run(pg, project_id, PLAN, fake, ledger=ledger, actor_id=actor_id, artifacts=artifacts)

    assert get_execution(pg, project_id, canned.id) is None
    assert ledger.events(pg, project_id) == []
