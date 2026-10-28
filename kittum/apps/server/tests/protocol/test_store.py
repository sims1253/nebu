import os
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psycopg
import pytest
from psycopg.rows import dict_row

from kittum.core import FixedClock, Id, KittumError, NotFoundError, new_id
from kittum.db.testing import null_tx
from kittum.ledger import PostgresLedger
from kittum.ledger.testing import InMemoryLedger
from kittum.protocol import (
    PUBLISHED_EVENT,
    PostgresProtocolStore,
    ProtocolStateError,
    ProtocolStore,
    compile,
    get_version,
    protocol_hash,
    publish,
    save_draft,
)
from kittum.protocol.testing import InMemoryProtocolStore
from tests.protocol.support import example_source, example_text, seed_project

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
    topic["audit"] = {"sentinel_percent": "1", "random_audit_percent": "10"}
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
    ledger = PostgresLedger()
    _publish_flow(PostgresProtocolStore(ledger), ledger, pg, project, approver)

    row = pg.execute(
        "select v.content_hash, e.type, e.payload ->> 'content_hash' as event_hash"
        " from protocol_version v join audit_event e on e.event_id = v.approved_event_id"
        " where v.project_id = %s order by v.created_at, v.id limit 1",
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
    publish(pg, project, draft.id, approver, ledger=PostgresLedger())
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
        publish(pg, project, draft.id, system, ledger=PostgresLedger())
    with pytest.raises(NotFoundError):
        publish(pg, project, new_id(), system, ledger=PostgresLedger())


def test_returned_protocols_cannot_change_what_the_store_holds() -> None:
    clock = FixedClock(datetime(2026, 10, 28, 9, 0, tzinfo=UTC))
    store = InMemoryProtocolStore(InMemoryLedger(clock), clock)
    project = new_id()
    version = compile(example_text())
    published = store.publish(
        null_tx(), project, store.save_draft(null_tx(), project, version).id, new_id()
    )

    with pytest.raises(TypeError, match="immutable"):
        published.version.protocol.criteria[0].text["en"] = "Children who smoke."
    with pytest.raises(TypeError, match="immutable"):
        version.protocol.questions[0].label.update(en="Everyone")
    with pytest.raises(TypeError, match="immutable"):
        published.version.protocol.extensions["note"] = "x"

    again = store.get_version(null_tx(), project, published.id)
    assert again.version.protocol.criteria[0].text["en"] == (
        "Adults (18 years or older) who smoke tobacco."
    )
    assert (
        protocol_hash(again.version.protocol)
        == again.version.content_hash
        == (version.content_hash)
    )


@pytest.mark.postgres
def test_concurrent_publications_cannot_skip_the_mode_change_reason(pg: Tx) -> None:
    """B's draft keeps M1. Against the M2 version A is publishing, that is a mode change."""
    del pg  # skips without a database; the test needs its own committing connections
    url = os.environ["KITTUM_DATABASE_URL"]
    ledger = PostgresLedger()
    with psycopg.connect(url, row_factory=dict_row) as setup:
        project, approver, _ = seed_project(setup)
        first = save_draft(setup, project, compile(example_text()))
        publish(setup, project, first.id, approver, ledger=ledger)
        to_m2 = save_draft(setup, project, compile(_switch_to_m2()))
        back_to_m1 = example_source()
        back_to_m1["version_label"] = "0.5"
        still_m1 = save_draft(setup, project, compile(back_to_m1))
        setup.commit()

    outcome: list[object] = []
    with (
        psycopg.connect(url, row_factory=dict_row) as a,
        psycopg.connect(url, row_factory=dict_row) as b,
        psycopg.connect(url, autocommit=True) as monitor,
    ):
        publish(a, project, to_m2.id, approver, ledger=ledger, reason="Validation pack approved.")

        def publish_b() -> None:
            try:
                outcome.append(publish(b, project, still_m1.id, approver, ledger=ledger))
                b.commit()
            except ProtocolStateError as e:
                outcome.append(e)
                b.rollback()

        worker = threading.Thread(target=publish_b)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            waiting = monitor.execute(
                "select 1 from pg_stat_activity where pid = %s and wait_event_type = 'Lock'",
                (b.info.backend_pid,),
            ).fetchone()
            if waiting:
                break
            time.sleep(0.02)
        a.commit()
        worker.join(timeout=10)

    (result,) = outcome
    assert isinstance(result, ProtocolStateError)
    assert "needs a reason (TRIALS M2→M1)" in str(result)
