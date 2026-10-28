import threading
from typing import TYPE_CHECKING

import pytest

from kittum.ledger import EventDraft, PostgresLedger
from kittum.ledger.testing import seed_project

if TYPE_CHECKING:
    from kittum.db import Database

pytestmark = pytest.mark.postgres


def test_concurrent_appends_to_one_project_form_one_chain(database: Database) -> None:
    ledger = PostgresLedger()
    with database.transaction() as tx:
        project, actor = seed_project(tx)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def writer(name: str) -> None:
        start.wait()
        try:
            for i in range(20):
                with database.transaction() as tx:
                    ledger.append(
                        tx,
                        EventDraft(
                            project_id=project,
                            type="test.concurrent",
                            schema_version=1,
                            actor_id=actor,
                            payload={"writer": name, "i": i},
                        ),
                    )
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with database.transaction() as tx:
        events = ledger.events(tx, project)
        assert [e.seq for e in events] == list(range(1, 41))
        assert {e.payload["writer"] for e in events} == {"a", "b"}
        assert ledger.verify(tx, project).ok
