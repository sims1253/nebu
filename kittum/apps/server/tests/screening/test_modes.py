"""M2 and M3 through the service: AI disagreements in M2, the AI's order and the stop test in M3."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.screening import Decision
from tests.screening.support import (
    NON_SMOKERS,
    OFF_TOPIC,
    RELEVANT,
    SCOPE,
    STAGE,
    build_world,
    recorded_backend,
    simulated_answers,
    timing,
)

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database
    from kittum.screening import DecideResult, QueueItem, StopStatus
    from tests.screening.support import World

pytestmark = pytest.mark.postgres


def queue(world: World, reviewer: Id, limit: int = 50) -> list[QueueItem]:
    with world.db.transaction() as tx:
        return world.screening.queue(tx, world.project_id, reviewer, SCOPE, STAGE, limit=limit)


def decide(
    world: World, reviewer: Id, item: QueueItem, outcome: str, reason: str | None = None
) -> DecideResult:
    """A provisional decision, kept as the final one if the AI is revealed."""
    decision = Decision(outcome=outcome, reason_criterion_id=reason)  # type: ignore[arg-type]
    with world.db.transaction() as tx:
        result = world.screening.decide(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            reviewer_id=reviewer,
            assignment_id=item.assignment_id,
            decision=decision,
            phase="provisional",
            expected_revision=item.revision,
            timing=timing(0),
        )
        if result.phase == "provisional":
            world.screening.decide(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=reviewer,
                assignment_id=item.assignment_id,
                decision=decision,
                phase="final",
                expected_revision=result.revision,
                timing=timing(0, revealed=True),
            )
    return result


def by_title(items: list[QueueItem], prefix: str) -> QueueItem:
    (found,) = [i for i in items if i.title.startswith(prefix)]
    return found


def test_m2_sends_every_ai_disagreement_to_adjudication(database: Database) -> None:
    world = build_world(database, "M2")
    keep = (
        "Nicotine patch versus",
        "Transdermal nicotine for active",
        "Varenicline",
        "Use of",
        "GLPG2737",
        "Rate and predictors",
    )
    world.records = [world.record(t) for t in keep]
    a = world.people.a
    # One screener; the audit plan samples 10% (one record) for the auditor.
    info = world.open(screeners=(a,), auditor=world.people.auditor)
    assert info.mode == "M2" and info.screeners == (a,)

    items = queue(world, a)
    # Agreement: both include. No conflict.
    assert decide(world, a, by_title(items, "Nicotine patch"), "include").phase == "final"
    # The human includes, the AI excludes on a verified quote, and the human keeps the inclusion.
    shown = decide(world, a, by_title(items, "Transdermal nicotine"), "include")
    assert shown.reveal is not None and shown.reveal.criterion_id == "POP"
    # The human excludes and the model answered not_met too; the filter's downgrade makes no
    # disagreement.
    assert decide(world, a, by_title(items, "Varenicline"), "exclude", "INT").reveal is None
    assert decide(world, a, by_title(items, "Use of"), "exclude", "DESIGN").reveal is None
    # The human excludes and the model saw every criterion met: a disagreement.
    glpg = decide(world, a, by_title(items, "GLPG2737"), "exclude", "POP")
    assert glpg.reveal is not None and glpg.reveal.status == "met"
    # Census: the human excludes, sees that the model found no failing criterion, and revises
    # to include. The provisional disagreement still goes to adjudication.
    prescription = by_title(items, "Rate and predictors")
    with world.db.transaction() as tx:
        first = world.screening.decide(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=prescription.assignment_id,
            decision=Decision(outcome="exclude", reason_criterion_id="DESIGN"),
            phase="provisional",
            expected_revision=0,
            timing=timing(0),
        )
        assert first.phase == "provisional"
        world.screening.decide(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=prescription.assignment_id,
            decision=Decision(outcome="include"),
            phase="final",
            expected_revision=first.revision,
            timing=timing(0, revealed=True),
        )

    with world.db.transaction() as tx:
        conflicts = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
        titles = sorted(c.title[:20] for c in conflicts)
        assert titles == ["GLPG2737 in lumacaft", "Rate and predictors ", "Transdermal nicotine"]
        assert all(len(c.columns) == 1 and c.columns[0].label == "A" for c in conflicts)
        colitis = next(c for c in conflicts if c.title.startswith("Transdermal"))
        result = world.screening.adjudicate(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            adjudicator_id=world.people.adjudicator,
            conflict_id=colitis.conflict_id,
            expected_revision=colitis.revision,
            decision=Decision(outcome="exclude", reason_criterion_id="POP"),
        )
        assert result.ai.excludes is True
        status = world.screening.status(tx, world.project_id, world.people.lead, SCOPE, STAGE)
        assert (status.screened, status.remaining, status.conflicts) == (6, 0, 2)
        assert status.metrics is not None
        assert [p.kind for p in status.metrics.agreement] == ["human_ai"]
        # Agreement is reported against the verified reading: only the patch trial agrees.
        assert status.metrics.agreement[0].items == 6
        assert status.metrics.agreement[0].pabak == pytest.approx(-2 / 3)
        assert world.ledger.verify(tx, world.project_id).ok


def test_m3_screens_in_ai_order_and_the_stop_test_fires(database: Database) -> None:
    # 150 simulated records, 8 relevant: the size where the test can stop after 100.
    world = build_world(database, "M3", simulate={RELEVANT: 8, NON_SMOKERS: 30, OFF_TOPIC: 112})
    a, b = world.people.a, world.people.b
    backend = recorded_backend(
        world.records, world.criteria(), answers=simulated_answers(world.records)
    )
    world.open(auditor=world.people.auditor, backend=backend)

    # Both humans see the same AI order: the AI's includes with every criterion met first, its
    # proposed exclusions last. The two sentinels (known includes) rank among the unclear ones.
    order_a = [i.title for i in queue(world, a, limit=200)]
    order_b = [i.title for i in queue(world, b, limit=200)]
    assert order_a == order_b and len(order_a) == 152
    assert all(t.startswith(RELEVANT) for t in order_a[:8])
    assert all(t.startswith(NON_SMOKERS) for t in order_a[-30:])

    def screen(n: int) -> None:
        for reviewer in (a, b):
            for item in queue(world, reviewer, limit=n):
                relevant = item.title.startswith(RELEVANT) or "Nicotine" in item.title[:10]
                decide(
                    world,
                    reviewer,
                    item,
                    "include" if relevant else "exclude",
                    None if relevant else "POP",
                )

    def stop() -> StopStatus:
        with world.db.transaction() as tx:
            status = world.screening.status(tx, world.project_id, world.people.lead, SCOPE, STAGE)
        assert status.metrics is not None and status.metrics.stop_test is not None
        return status.metrics.stop_test

    screen(60)
    early = stop()
    assert early.screened >= 58 and early.relevant == 8
    assert not early.stop and not early.minimum_met

    screen(42)
    late = stop()
    assert late.screened == 100 and late.relevant == 8
    assert late.minimum_met and late.stop
    assert late.p_value < 0.05
    with world.db.transaction() as tx:
        assert world.ledger.verify(tx, world.project_id).ok
