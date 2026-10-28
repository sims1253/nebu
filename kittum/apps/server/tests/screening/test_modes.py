"""M2 and M3 through the service: AI disagreements in M2, the AI's order and the stop test in M3."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.protocol import StoppingPlan
from kittum.screening import AI_DISAGREEMENT_RESCREEN, Decision, stop_status
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
            expected_etag=item.etag,
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
                expected_etag=result.etag,
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
            expected_etag=prescription.etag,
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
            expected_etag=first.etag,
            timing=timing(0, revealed=True),
        )

    # No AI-only disagreement reaches adjudication: the three records the AI disputed (colitis,
    # GLPG2737, and the census case) go to the auditor for a blind extra screen instead.
    with world.db.transaction() as tx:
        assert (
            world.screening.adjudication_queue(
                tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
            )
            == []
        )
        outcomes = world.screening.outcomes(tx, world.project_id, SCOPE, STAGE)
        rescreened = [
            e.payload
            for e in world.ledger.events(tx, world.project_id)
            if e.type == AI_DISAGREEMENT_RESCREEN
        ]
    assert [p["reason"] for p in rescreened] == ["ai_disagreement"] * 3
    titles = {r.item_ref: r.title[:20] for r in world.records}
    assert sorted(titles[p["item_ref"]] for p in rescreened) == [
        "GLPG2737 in lumacaft",
        "Rate and predictors ",
        "Transdermal nicotine",
    ]
    assert all(outcomes[p["item_ref"]] is None for p in rescreened)  # waiting for the auditor

    # The auditor screens their whole stream blind, as they judge each record.
    truth = {
        "Nicotine patch": ("include", None),
        "Transdermal nicotine": ("exclude", "POP"),
        "Varenicline": ("exclude", "INT"),
        "Use of": ("exclude", "DESIGN"),
        "GLPG2737": ("exclude", "POP"),
        "Rate and predictors": ("include", None),
        "Nicotine lozenge": ("include", None),
        "Nicotine mouth spray": ("include", None),
    }
    auditor = world.people.auditor
    for item in queue(world, auditor):
        outcome, reason = next(v for k, v in truth.items() if item.title.startswith(k))
        decide(world, auditor, item, outcome, reason)

    with world.db.transaction() as tx:
        # The auditor excluded the colitis trial the human included: a human disagreement.
        (conflict,) = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
        assert conflict.title.startswith("Transdermal nicotine")
        assert sorted(c.outcome for c in conflict.columns) == ["exclude", "include"]
        result = world.screening.adjudicate(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            adjudicator_id=world.people.adjudicator,
            conflict_id=conflict.conflict_id,
            expected_etag=conflict.etag,
            decision=Decision(outcome="exclude", reason_criterion_id="POP"),
        )
        assert result.ai.excludes is True  # the AI column opens after the commit
        outcomes = world.screening.outcomes(tx, world.project_id, SCOPE, STAGE)
        final_outcomes = {r.title[:20]: outcomes[r.item_ref] for r in world.records}
        # The auditor agreed on GLPG2737 and the census case: the human decisions stand.
        assert final_outcomes == {
            "Nicotine patch versu": "include",
            "Transdermal nicotine": "exclude",
            "Varenicline versus p": "exclude",
            "Use of nicotine repl": "exclude",
            "GLPG2737 in lumacaft": "exclude",
            "Rate and predictors ": "include",
        }
        status = world.screening.status(tx, world.project_id, world.people.lead, SCOPE, STAGE)
        assert (status.screened, status.remaining, status.conflicts) == (6, 0, 0)
        assert status.metrics is not None
        assert status.metrics.rescreens.model_dump() == {
            "triggered": 3,
            "pending": 0,
            "upheld": 2,
            "adjudicated": 1,
        }
        own, verified = status.metrics.agreement
        assert (own.kind, verified.kind) == ("human_ai", "human_ai_verified")
        # The model's own reading agrees on the patch trial and the two exclusions it shares;
        # the verified reading agrees only on the patch trial.
        assert own.items == verified.items == 6
        assert own.pabak == pytest.approx(0.0)
        assert verified.pabak == pytest.approx(-2 / 3)
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

    slipped: list[str] = []

    def screen(n: int) -> None:
        for reviewer in (a, b):
            for item in queue(world, reviewer, limit=n):
                relevant = item.title.startswith(RELEVANT) or "Nicotine" in item.title[:10]
                if reviewer == b and not relevant and not slipped:  # B's one slip: a conflict
                    slipped.append(item.title)
                    relevant = True
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
    assert early.screened >= 58 and early.relevant == 9  # the open conflict counts as relevant
    assert not early.stop and not early.minimum_met
    assert (early.verdict, early.pending_conflicts) == ("pending_conflicts", 1)

    screen(42)
    late = stop()
    # Sentinels sit among the unclear records in a seeded order, so 100 or a few more of the
    # records are screened.
    assert late.screened >= 100 and late.minimum_met
    # The test would pass with the open conflict counted as relevant; certification waits.
    assert late.p_value < 0.05
    assert (late.verdict, late.pending_conflicts, late.stop) == ("pending_conflicts", 1, False)
    with world.db.transaction() as tx:
        (conflict,) = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
        assert conflict.title == slipped[0]
        world.screening.adjudicate(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            adjudicator_id=world.people.adjudicator,
            conflict_id=conflict.conflict_id,
            expected_etag=conflict.etag,
            decision=Decision(outcome="exclude", reason_criterion_id="POP"),
        )
    certified = stop()
    assert certified.relevant == 8 and certified.pending_conflicts == 0
    assert (certified.verdict, certified.stop) == ("stop", True)
    assert certified.p_value < 0.05
    with world.db.transaction() as tx:
        assert world.ledger.verify(tx, world.project_id).ok


def test_m3_stop_waits_for_conflicts_in_the_tested_prefix() -> None:
    """The review's reproduction: 750 records, 500 screened, the first 40 in conflict.

    Counted as relevant, the 40 open conflicts would let the test pass; resolved as the
    exclusions they may turn out to be, it fails. So certification waits for them.
    """
    plan = StoppingPlan(
        method="buscar",
        target_recall_percent="95",
        confidence_percent="95",
        minimum_screened_percent="50",
    )
    in_conflict = stop_status(plan, ["conflict"] * 40 + ["exclude"] * 460 + [None] * 250)
    assert in_conflict.p_value == pytest.approx(0.0433, abs=5e-5)  # would have passed
    assert (in_conflict.verdict, in_conflict.pending_conflicts) == ("pending_conflicts", 40)
    assert not in_conflict.stop

    resolved = stop_status(plan, ["exclude"] * 500 + [None] * 250)
    assert resolved.p_value == pytest.approx(1 / 3)
    assert (resolved.verdict, resolved.pending_conflicts, resolved.stop) == ("continue", 0, False)
