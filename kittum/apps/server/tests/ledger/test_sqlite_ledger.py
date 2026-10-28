import sqlite3
from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.db.testing import null_tx
from kittum.ledger import EventDraft, SqliteLedger

if TYPE_CHECKING:
    from pathlib import Path


def test_appends_commit_and_roll_back_with_the_callers_transaction(tmp_path: Path) -> None:
    ledger = SqliteLedger.open(tmp_path / "ledger.sqlite")
    conn = ledger.connection
    conn.execute("create table decision (record text primary key, outcome text not null)")
    project, actor = new_id(), new_id()

    def decide(tx: object, record: str, outcome: str) -> None:
        ledger.append(
            tx,  # type: ignore[arg-type]
            EventDraft(
                project_id=project,
                type="screening.decided",
                schema_version=1,
                actor_id=actor,
                payload={"record": record, "outcome": outcome},
            ),
        )
        tx.execute("insert into decision values (?, ?)", (record, outcome))  # type: ignore[attr-defined]

    # The event is appended, then the state change fails: neither may remain.
    with pytest.raises(sqlite3.IntegrityError), ledger.transaction() as tx:
        decide(tx, "PMID:31562797", "include")
        decide(tx, "PMID:31562797", "exclude")
    assert ledger.events(null_tx(), project) == []
    assert conn.execute("select count(*) from decision").fetchone()[0] == 0

    with ledger.transaction() as tx:
        decide(tx, "PMID:31562797", "include")
    assert [e.payload["outcome"] for e in ledger.events(null_tx(), project)] == ["include"]
    assert [tuple(r) for r in conn.execute("select * from decision")] == [
        ("PMID:31562797", "include")
    ]
    assert ledger.verify(null_tx(), project).ok
    ledger.close()
