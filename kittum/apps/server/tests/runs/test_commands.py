"""The decider path: one transaction for events, rows, jobs, and the receipt."""

import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from kittum.core import FixedClock, new_id
from kittum.db.testing import null_tx
from kittum.ledger import EventDraft, PostgresLedger
from kittum.ledger.testing import InMemoryLedger, seed_project
from kittum.runs import (
    Command,
    CommandIdReused,
    CommandRejected,
    Commands,
    Decision,
    JobSpec,
    PostgresJobQueue,
    PostgresReceipts,
)
from kittum.runs.testing import InMemoryJobQueue, InMemoryReceipts
from tests.runs.conftest import START

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kittum.core import Id
    from kittum.db import Database, Tx
    from kittum.ledger import AuditEvent, Ledger
    from kittum.runs import JobQueue

LEASE = timedelta(minutes=1)


@dataclass
class Screening:
    """A stand-in module: one decision per record, written through `Commands`."""

    kind: str
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    decide_calls: int = 0
    fail_apply: bool = False

    def load(self, _tx: Tx, command: Command) -> dict[str, Any] | None:
        return self.decisions.get(command.payload["record"])

    def decide(self, command: Command, existing: dict[str, Any] | None) -> Decision[dict]:
        self.decide_calls += 1
        time.sleep(0.02)  # Widen the window for concurrent retries.
        record = command.payload["record"]
        if existing is not None:
            raise CommandRejected(f"{record} is already decided")
        draft = {"project_id": command.project_id, "actor_id": command.actor_id}
        return Decision(
            events=(
                EventDraft(
                    **draft, type="screening.decided", schema_version=1, payload=command.payload
                ),
                EventDraft(
                    **draft,
                    type="screening.audit_sampled",
                    schema_version=1,
                    payload={"record": record},
                ),
            ),
            jobs=(
                JobSpec(
                    project_id=command.project_id,
                    kind=self.kind,
                    stage_key=f"{self.kind}:audit:{record}",
                    payload={"record": record},
                ),
            ),
            changes={"record": record, "outcome": command.payload["outcome"]},
        )

    def apply(self, _tx: Tx, decision: Decision[dict], events: tuple[AuditEvent, ...]) -> None:
        if self.fail_apply:
            raise RuntimeError("disk full")
        assert decision.changes is not None
        self.decisions[decision.changes["record"]] = {
            **decision.changes,
            "event_seq": events[0].seq,
        }


@dataclass
class Setup:
    commands: Commands
    ledger: Ledger
    queue: JobQueue
    tx: Tx
    project_id: Id
    actor_id: Id
    screening: Screening

    def command(self, record: str, outcome: str = "exclude", **kw: Any) -> Command:
        return Command(
            command_id=kw.get("command_id", new_id()),
            project_id=self.project_id,
            type=kw.get("type", "screening.decide"),
            actor_id=self.actor_id,
            payload={"record": record, "outcome": outcome},
        )

    def execute(self, command: Command) -> Any:
        return self.commands.execute(
            self.tx,
            command,
            self.screening.decide,
            load=self.screening.load,
            apply=self.screening.apply,
        )


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.postgres)])
def setup(request: pytest.FixtureRequest) -> Iterator[Setup]:
    clock = FixedClock(START)
    screening = Screening(kind=f"test.{new_id()}")
    if request.param == "memory":
        ledger, queue = InMemoryLedger(clock), InMemoryJobQueue(clock)
        commands = Commands(ledger, queue, InMemoryReceipts(), clock)
        yield Setup(commands, ledger, queue, null_tx(), new_id(), new_id(), screening)
        return
    pg: Tx = request.getfixturevalue("pg")
    project, actor = seed_project(pg)
    ledger, queue = PostgresLedger(clock), PostgresJobQueue(clock)
    commands = Commands(ledger, queue, PostgresReceipts(), clock)
    yield Setup(commands, ledger, queue, pg, project, actor, screening)


def _claim_all(s: Setup) -> list[str]:
    out = []
    while job := s.queue.claim(s.tx, (s.screening.kind,), "w", LEASE):
        out.append(job.spec.stage_key)
    return out


def test_a_replayed_command_is_a_no_op(setup: Setup) -> None:
    command = setup.command("PMID:31562797")
    receipt = setup.execute(command)
    assert receipt.event_seqs == (1, 2)
    assert setup.screening.decisions["PMID:31562797"]["event_seq"] == 1

    # The client times out and sends the same command again.
    replay = setup.execute(command)

    assert replay == receipt
    assert setup.screening.decide_calls == 1
    assert [e.type for e in setup.ledger.events(setup.tx, setup.project_id)] == [
        "screening.decided",
        "screening.audit_sampled",
    ]
    assert _claim_all(setup) == [f"{setup.screening.kind}:audit:PMID:31562797"]


def test_a_new_command_is_decided_against_current_state(setup: Setup) -> None:
    setup.execute(setup.command("PMID:31562797"))
    with pytest.raises(CommandRejected):
        setup.execute(setup.command("PMID:31562797", outcome="include"))
    other = setup.execute(setup.command("NCT02578680"))
    assert other.event_seqs == (3, 4)


def test_a_command_id_cannot_be_reused_for_another_command(setup: Setup) -> None:
    command = setup.command("PMID:31562797")
    setup.execute(command)
    reused = setup.command("PMID:1", command_id=command.command_id, type="screening.adjudicate")
    with pytest.raises(CommandIdReused):
        setup.execute(reused)


@pytest.mark.postgres
def test_a_failed_command_writes_nothing_and_can_be_retried(database: Database) -> None:
    screening = Screening(kind=f"test.{new_id()}", fail_apply=True)
    ledger, queue, receipts = PostgresLedger(), PostgresJobQueue(), PostgresReceipts()
    commands = Commands(ledger, queue, receipts)
    with database.transaction() as tx:
        project, actor = seed_project(tx)
    command = Command(
        command_id=new_id(),
        project_id=project,
        type="screening.decide",
        actor_id=actor,
        payload={"record": "PMID:31562797", "outcome": "exclude"},
    )

    def run() -> Any:
        with database.transaction() as tx:
            return commands.execute(
                tx, command, screening.decide, load=screening.load, apply=screening.apply
            )

    with pytest.raises(RuntimeError, match="disk full"):
        run()
    with database.transaction() as tx:
        assert ledger.events(tx, project) == []
        assert receipts.find(tx, command.command_id) is None
        assert queue.claim(tx, (screening.kind,), "w", LEASE) is None

    screening.fail_apply = False
    assert run().event_seqs == (1, 2)


@pytest.mark.postgres
def test_concurrent_retries_of_one_command_decide_once(database: Database) -> None:
    screening = Screening(kind=f"test.{new_id()}")
    ledger = PostgresLedger()
    commands = Commands(ledger, PostgresJobQueue(), PostgresReceipts())
    with database.transaction() as tx:
        project, actor = seed_project(tx)
    command = Command(
        command_id=new_id(),
        project_id=project,
        type="screening.decide",
        actor_id=actor,
        payload={"record": "PMID:31562797", "outcome": "exclude"},
    )
    start = threading.Barrier(2)
    receipts: list[Any] = []

    def retry() -> None:
        start.wait()
        with database.transaction() as tx:
            receipts.append(
                commands.execute(
                    tx, command, screening.decide, load=screening.load, apply=screening.apply
                )
            )

    threads = [threading.Thread(target=retry) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(receipts) == 2 and receipts[0] == receipts[1]
    assert screening.decide_calls == 1
    with database.transaction() as tx:
        assert [e.seq for e in ledger.events(tx, project)] == [1, 2]


@pytest.mark.postgres
def test_conflicting_commands_with_different_ids_decide_one_after_the_other(
    database: Database,
) -> None:
    """Two reviewers claim the one adjudication slot of a record at the same moment."""
    ledger = PostgresLedger()
    commands = Commands(ledger, PostgresJobQueue(), PostgresReceipts())
    with database.transaction() as tx:
        project, actor = seed_project(tx)

    def load(tx: Tx, _command: Command) -> bool:
        return any(e.type == "adjudication.claimed" for e in ledger.events(tx, project))

    def decide(command: Command, claimed: bool) -> Decision:
        time.sleep(0.05)  # Both would pass here if they had read the same state.
        if claimed:
            raise CommandRejected("the slot is taken")
        return Decision(
            events=(
                EventDraft(
                    project_id=command.project_id,
                    type="adjudication.claimed",
                    schema_version=1,
                    actor_id=command.actor_id,
                    payload=command.payload,
                ),
            )
        )

    start = threading.Barrier(2)
    outcomes: list[str] = []

    def claim(reviewer: str) -> None:
        command = Command(
            command_id=new_id(),
            project_id=project,
            type="adjudication.claim",
            actor_id=actor,
            payload={"record": "PMID:31562797", "reviewer": reviewer},
        )
        start.wait()
        try:
            with database.transaction() as tx:
                commands.execute(tx, command, decide, load=load)
            outcomes.append("claimed")
        except CommandRejected:
            outcomes.append("rejected")

    threads = [threading.Thread(target=claim, args=(r,)) for r in ("anna", "ben")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert sorted(outcomes) == ["claimed", "rejected"]
    with database.transaction() as tx:
        assert [e.type for e in ledger.events(tx, project)] == ["adjudication.claimed"]
