"""A retried "wrong document" that arrives while the first request is still running.

The workbench retries a request whose answer it did not get, with the same command ID. The retry
must wait for the first one, then return its result: one withdrawal, one detachment, one ledger
event, and no database error. Runs on a committing database, with two transactions at once.
"""

import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pymupdf
import pytest

from kittum.app import FULL_TEXT_DETACHED, attach_full_text, mark_wrong_document
from kittum.core import new_id
from tests.screening.support import SCOPE, build_world

if TYPE_CHECKING:
    from kittum.app import AppContext, WrongDocument
    from kittum.db import Database

pytestmark = pytest.mark.postgres


def checklist_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "CONSORT checklist of information to include")
    return doc.tobytes()


def test_a_retry_during_the_first_request_returns_its_result(database: Database) -> None:
    world = build_world(database)
    record = world.record("Nicotine patch versus")
    world.records = [record, world.record("Nicotine gum")]
    world.open(stage="full_text")
    with world.db.transaction() as tx:
        attached = attach_full_text(
            tx,
            world.project_id,
            record.item_ref,
            checklist_pdf(),
            actor_id=world.people.lead,
            artifacts=world.artifacts,
            ledger=world.ledger,
        )
        (item,) = [
            i
            for i in world.screening.queue(
                tx, world.project_id, world.people.a, SCOPE, "full_text", limit=50
            )
            if i.title == record.title
        ]
    ctx = cast(
        "AppContext",
        SimpleNamespace(screening=lambda: world.screening, require_ledger=lambda: world.ledger),
    )
    command_id = new_id()

    def mark(tx: Any) -> WrongDocument:
        return mark_wrong_document(
            tx,
            ctx,
            world.project_id,
            command_id=command_id,
            reviewer_id=world.people.a,
            assignment_id=item.assignment_id,
            expected_etag=item.etag,
            document_id=attached.document_id,
            note="A checklist",
        )

    marked = threading.Event()
    results: dict[str, Any] = {}

    def first() -> None:
        with world.db.transaction() as tx:
            results["first"] = mark(tx)
            marked.set()
            time.sleep(0.5)  # the retry arrives now, before this commits

    thread = threading.Thread(target=first)
    thread.start()
    assert marked.wait(30)
    with world.db.transaction() as tx:
        results["retry"] = mark(tx)
    thread.join()

    assert results["retry"] == results["first"]
    with world.db.transaction() as tx:
        events = [
            e for e in world.ledger.events(tx, world.project_id) if e.type == FULL_TEXT_DETACHED
        ]
        rows = tx.execute(
            "select count(*) as n from full_text_detachment where command_id = %s", (command_id,)
        ).fetchone()
        assert world.ledger.verify(tx, world.project_id).ok
    assert len(events) == 1
    assert rows is not None and rows["n"] == 1
