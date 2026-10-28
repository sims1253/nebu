from datetime import UTC, datetime

from kittum.core import FixedClock, new_id
from kittum.db.testing import null_tx
from kittum.ledger import GENESIS_HASH, AuditEvent, EventDraft
from kittum.ledger.testing import InMemoryLedger


def _ledger() -> InMemoryLedger:
    return InMemoryLedger(FixedClock(datetime(2026, 10, 28, 9, 0, tzinfo=UTC)))


def test_events_chain_from_genesis() -> None:
    ledger = _ledger()
    project, actor = new_id(), new_id()
    first = ledger.append(
        null_tx(),
        EventDraft(project_id=project, type="t", schema_version=1, actor_id=actor, payload={}),
    )
    second = ledger.append(
        null_tx(),
        EventDraft(
            project_id=project, type="t", schema_version=1, actor_id=actor, payload={"a": 1}
        ),
    )
    assert (first.seq, second.seq) == (1, 2)
    assert first.prev_hash == GENESIS_HASH
    assert second.prev_hash == first.event_hash
    assert ledger.verify(null_tx(), project).ok


def test_verify_finds_a_rewritten_payload() -> None:
    ledger = _ledger()
    project, actor = new_id(), new_id()
    for i in range(3):
        ledger.append(
            null_tx(),
            EventDraft(
                project_id=project, type="t", schema_version=1, actor_id=actor, payload={"i": i}
            ),
        )
    events = ledger._events[project]
    events[1] = AuditEvent.model_validate({**events[1].model_dump(), "payload": {"i": 99}})
    report = ledger.verify(null_tx(), project)
    assert not report.ok
    assert report.first_bad_seq == 2
    assert report.checked_through_seq == 1


def test_projects_have_separate_chains() -> None:
    ledger = _ledger()
    a, b, actor = new_id(), new_id(), new_id()
    ledger.append(
        null_tx(), EventDraft(project_id=a, type="t", schema_version=1, actor_id=actor, payload={})
    )
    event = ledger.append(
        null_tx(), EventDraft(project_id=b, type="t", schema_version=1, actor_id=actor, payload={})
    )
    assert event.seq == 1
    assert event.prev_hash == GENESIS_HASH


def test_verify_finds_a_swapped_payload_hash() -> None:
    ledger = _ledger()
    project, actor = new_id(), new_id()
    ledger.append(
        null_tx(),
        EventDraft(project_id=project, type="t", schema_version=1, actor_id=actor, payload={}),
    )
    events = ledger._events[project]
    events[0] = AuditEvent.model_validate({**events[0].model_dump(), "payload_hash": "f" * 64})
    assert not ledger.verify(null_tx(), project).ok


def test_event_hash_ignores_the_timezone_of_the_same_instant() -> None:
    from datetime import timedelta, timezone

    from kittum.ledger import event_hash

    utc = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    berlin = utc.astimezone(timezone(timedelta(hours=1)))
    common = {
        "project_id": new_id(),
        "seq": 1,
        "event_id": new_id(),
        "type": "t",
        "schema_version": 1,
        "actor_id": new_id(),
        "payload_hash": "a" * 64,
        "prev_hash": GENESIS_HASH,
    }
    assert event_hash(recorded_at=utc, **common) == event_hash(recorded_at=berlin, **common)
