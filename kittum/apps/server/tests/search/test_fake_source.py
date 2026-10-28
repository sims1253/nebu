from kittum.core import new_id, sha256_hex
from kittum.db.testing import null_tx
from kittum.ledger.testing import InMemoryLedger
from kittum.search import SearchPlan, SourceRecord, run
from kittum.search.testing import FakeSource


def test_fake_source_runs_and_records_plans() -> None:
    rec = SourceRecord(
        occurrence_id=new_id(),
        source="test",
        provider_record_id="rec-1",
        ordinal=0,
        title="Test Title",
        abstract="Test Abstract",
        raw_hash=sha256_hex(b"test"),
    )
    fake = FakeSource(name="custom-fake", records=(rec,), query_translation="translated")

    plan = SearchPlan(
        topic_id="t1",
        source="custom-fake",
        platform="TestPlatform",
        strategy="my search",
    )
    tx = null_tx()
    ledger = InMemoryLedger()
    project_id = new_id()

    execution = run(tx, project_id, plan, fake, ledger=ledger)

    assert execution.reported_count == 1
    assert execution.retrieved_count == 1
    assert execution.query_translation == "translated"
    assert len(execution.records) == 1
    assert execution.records[0].title == "Test Title"
    assert len(fake.plans) == 1
    assert fake.plans[0].strategy == "my search"

    events = ledger.events(tx, project_id)
    assert len(events) == 1
    assert events[0].type == "search.executed"
