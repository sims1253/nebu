from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psycopg
import pytest

from kittum.core import FixedClock, Id, KittumError, NotFoundError, new_id
from kittum.db.testing import null_tx
from kittum.ledger.testing import InMemoryLedger
from kittum.protocol import (
    PUBLISHED_EVENT,
    PostgresProtocolStore,
    ProtocolStateError,
    ProtocolStore,
    compile,
    get_version,
    publish,
    save_draft,
)
from kittum.protocol.testing import InMemoryProtocolStore
from tests.protocol.support import SqlLedger, example_source, example_text, seed_project

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import Ledger


def _switch_to_m2() -> dict:
    source = example_source()
    source["version_label"] = "0.4"
    topic = source["search_topics"][0]
    topic["mode"] = "M2"
    topic["mode_rationale"] = {"en": "The update search returned 6,000 records."}
    topic["validation_pack"] = "kittum-validation-pack/2026-10"
    return source


def _publish_flow(store: ProtocolStore, ledger: Ledger, tx: Tx, project: Id, approver: Id) -> None:
    """The rules both stores follow: idempotent drafts, one publication, reasons for mode changes."""
    v1 = compile(example_text())
    draft = store.save_draft(tx, project, v1)
    assert (draft.status, draft.parent_id, draft.version) == ("draft", None, v1)
    assert store.save_draft(tx, project, v1).id == draft.id
    assert store.latest_published(tx, project) is None

    published = store.publish(tx, project, draft.id, approver)
    assert published.status == "published" and published.approved_by == approver
    assert store.get_version(tx, project, draft.id) == published
    assert store.latest_published(tx, project) == published
    (event,) = ledger.events(tx, project)
    assert event.event_id == published.approved_event_id
    assert (event.type, event.actor_id, event.recorded_at) == (
        PUBLISHED_EVENT,
        approver,
        published.published_at,
    )
    assert event.payload["content_hash"] == v1.content_hash
    assert event.payload["version_label"] == "0.3"
    assert event.payload["previous_version_id"] is None

    with pytest.raises(ProtocolStateError, match="already published"):
        store.publish(tx, project, draft.id, approver)

    v2 = store.save_draft(tx, project, compile(_switch_to_m2()))
    assert v2.parent_id == published.id
    with pytest.raises(ProtocolStateError, match=r"needs a reason \(TRIALS M1→M2\)"):
        store.publish(tx, project, v2.id, approver)
    reason = "Validation pack approved; the update search is too large for two screeners."
    v2 = store.publish(tx, project, v2.id, approver, reason=reason)
    second = ledger.events(tx, project)[-1]
    assert second.payload["reason"] == reason
    assert second.payload["mode_changes"] == [{"topic_id": "TRIALS", "before": "M1", "after": "M2"}]
    assert second.payload["previous_version_id"] == str(published.id)
    assert store.latest_published(tx, project) == v2
    assert [s.id for s in store.list_versions(tx, project)] == [published.id, v2.id]
    assert ledger.verify(tx, project).ok

    with pytest.raises(NotFoundError):
        store.get_version(tx, new_id(), published.id)


def test_in_memory_store_follows_the_publish_rules() -> None:
    clock = FixedClock(datetime(2026, 10, 28, 9, 0, tzinfo=UTC))
    ledger = InMemoryLedger(clock)
    _publish_flow(InMemoryProtocolStore(ledger, clock), ledger, null_tx(), new_id(), new_id())


@pytest.mark.postgres
def test_example_publishes_into_postgres_with_a_ledger_event(pg: Tx) -> None:
    project, approver, _ = seed_project(pg)
    ledger = SqlLedger()
    _publish_flow(PostgresProtocolStore(ledger), ledger, pg, project, approver)

    row = pg.execute(
        "select v.content_hash, e.type, e.payload ->> 'content_hash' as event_hash"
        " from protocol_version v join audit_event e on e.event_id = v.approved_event_id"
        " where v.project_id = %s order by v.created_at limit 1",
        (project,),
    ).fetchone()
    assert row == {
        "content_hash": compile(example_text()).content_hash,
        "type": PUBLISHED_EVENT,
        "event_hash": compile(example_text()).content_hash,
    }


@pytest.mark.postgres
def test_published_versions_are_frozen(pg: Tx) -> None:
    project, approver, _ = seed_project(pg)
    draft = save_draft(pg, project, compile(example_text()))
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), pg.transaction():
        pg.execute("update protocol_version set document = '{}'::jsonb where id = %s", (draft.id,))
    publish(pg, project, draft.id, approver, ledger=SqlLedger())
    for statement in (
        "update protocol_version set publish_reason = 'x' where id = %s",
        "delete from protocol_version where id = %s",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="frozen"), pg.transaction():
            pg.execute(statement, (draft.id,))


@pytest.mark.postgres
def test_a_tampered_document_is_detected_on_read(pg: Tx) -> None:
    project, _, _ = seed_project(pg)
    draft = save_draft(pg, project, compile(example_text()))
    pg.execute("alter table protocol_version disable trigger protocol_version_frozen")
    pg.execute(
        "update protocol_version set document = jsonb_set(document, '{version_label}', '\"9\"')"
        " where id = %s",
        (draft.id,),
    )
    with pytest.raises(KittumError, match="does not match its content hash"):
        get_version(pg, project, draft.id)


@pytest.mark.postgres
def test_only_a_human_can_approve(pg: Tx) -> None:
    project, _, system = seed_project(pg)
    draft = save_draft(pg, project, compile(example_text()))
    with pytest.raises(ProtocolStateError, match="human actor"):
        publish(pg, project, draft.id, system, ledger=SqlLedger())
    with pytest.raises(NotFoundError):
        publish(pg, project, new_id(), system, ledger=SqlLedger())
