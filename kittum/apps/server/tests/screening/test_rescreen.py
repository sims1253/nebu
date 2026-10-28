"""AI-only disagreements: a blind extra screen in the audit stream, never adjudication."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.screening import AI_DISAGREEMENT_RESCREEN, Decision
from tests.screening.support import SCOPE, SENTINELS, STAGE, build_world, timing

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database
    from kittum.screening import QueueItem
    from tests.screening.support import World

pytestmark = pytest.mark.postgres

# How a careful reviewer judges each record, by title prefix.
TRUTH = {
    "Nicotine patch versus": ("include", None),
    "Nicotine gum for": ("include", None),
    "Combination nicotine": ("include", None),
    "Transdermal nicotine for active": ("exclude", "POP"),
    "Varenicline versus": ("exclude", "INT"),
    "Use of nicotine replacement": ("exclude", "DESIGN"),
    "Efficacy and safety of lumacaftor": ("exclude", "POP"),
    "Lumacaftor/Ivacaftor: A Review": ("exclude", "DESIGN"),
    "Long-Term Impact of Lumacaftor": ("exclude", "POP"),
    "Impact of lumacaftor/ivacaftor and": ("exclude", "POP"),
    "Lumacaftor/Ivacaftor in Patients Aged": ("exclude", "POP"),
    "GLPG2737 in lumacaftor": ("exclude", "POP"),  # the model calls every criterion met
}
GLPG = "GLPG2737 in lumacaftor"
COLITIS = "Transdermal nicotine for active"  # the AI verifiably excludes it


def judged(title: str) -> Decision:
    if title in {s.record.title for s in SENTINELS}:
        return Decision(outcome="include")
    outcome, reason = next(v for k, v in TRUTH.items() if title.startswith(k))
    return Decision(outcome=outcome, reason_criterion_id=reason)  # type: ignore[arg-type]


def settle(world: World, reviewer: Id, item: QueueItem, decision: Decision) -> None:
    """Decide, keeping the decision through a reveal."""
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


def rescreen_events(world: World) -> list[dict[str, object]]:
    with world.db.transaction() as tx:
        return [
            e.payload
            for e in world.ledger.events(tx, world.project_id)
            if e.type == AI_DISAGREEMENT_RESCREEN
        ]


def assert_only_human_disagreements(world: World) -> list[str]:
    """Every adjudication item shows humans who disagree; returns their titles."""
    with world.db.transaction() as tx:
        queue = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
        kinds = tx.execute(
            "select c.kinds from screening_conflict c join screening_stage s"
            " on s.id = c.stage_id where s.project_id = %s",
            (world.project_id,),
        ).fetchall()
    for item in queue:
        assert len({c.outcome == "exclude" for c in item.columns}) == 2
    human = {"human_disagreement", "exclusion_reason", "audit_disagreement"}
    assert all(set(k["kinds"]) <= human for k in kinds)
    return [i.title for i in queue]


def the_auditor_screens(world: World, overrides: dict[str, Decision]) -> list[QueueItem]:
    """The auditor works through their stream; returns it as they saw it."""
    stream = world.screening_queue(world.people.auditor)
    for item in stream:
        decision = next(
            (d for k, d in overrides.items() if item.title.startswith(k)), judged(item.title)
        )
        settle(world, world.people.auditor, item, decision)
    return stream


@pytest.mark.parametrize("auditor_agrees", [True, False], ids=["upheld", "disputed"])
def test_m1_ai_include_goes_to_a_blind_extra_screen_in_the_audit_stream(
    database: Database, auditor_agrees: bool
) -> None:
    world = build_world(database)
    world.records = [world.record(t) for t in TRUTH]
    world.open()
    for reviewer in (world.people.a, world.people.b):
        for item in world.screening_queue(reviewer):
            settle(world, reviewer, item, judged(item.title))

    # Both humans excluded GLPG2737 while the model saw no failing criterion. Not adjudication:
    # a ledger-recorded extra screen by the auditor.
    (event,) = rescreen_events(world)
    assert (event["reason"], event["item_ref"]) == ("ai_include", world.record(GLPG).item_ref)
    assert event["auditor_id"] == str(world.people.auditor)
    assert assert_only_human_disagreements(world) == []

    # In the auditor's stream it sits among the random audit samples and the sentinel, alike.
    stream = world.screening_queue(world.people.auditor)
    assert sum(i.title.startswith(GLPG) for i in stream) == 1
    others = [i for i in stream if not i.title.startswith(GLPG)]
    assert len(others) >= 2  # at least one random audit sample besides it, and the sentinel
    assert len({frozenset(i.model_dump()) for i in stream}) == 1
    assert len({i.phase for i in stream}) == 1 and all(i.reveal is None for i in stream)

    overrides = {} if auditor_agrees else {GLPG: Decision(outcome="include")}
    the_auditor_screens(world, overrides)
    with world.db.transaction() as tx:
        outcome = world.screening.outcomes(tx, world.project_id, SCOPE, STAGE)[
            world.record(GLPG).item_ref
        ]
        status = world.screening.status(tx, world.project_id, world.people.lead, SCOPE, STAGE)
    assert status.metrics is not None
    gauge = status.metrics.rescreens
    if auditor_agrees:
        # The extra screen agrees with the humans: their exclusion stands.
        assert outcome == "exclude"
        assert assert_only_human_disagreements(world) == []
        assert (gauge.upheld, gauge.adjudicated) == (1, 0)
    else:
        # The auditor includes: now humans disagree, and that goes to adjudication, with the
        # three human decisions side by side and no AI.
        assert outcome == "conflict"
        (title,) = assert_only_human_disagreements(world)
        assert title.startswith(GLPG)
        assert (gauge.upheld, gauge.adjudicated) == (0, 1)
    with world.db.transaction() as tx:
        assert world.ledger.verify(tx, world.project_id).ok


@pytest.mark.parametrize("auditor_agrees", [True, False], ids=["upheld", "disputed"])
def test_m2_ai_disagreement_goes_to_a_blind_extra_screen(
    database: Database, auditor_agrees: bool
) -> None:
    world = build_world(database, "M2")
    world.records = [world.record(t) for t in TRUTH if t != GLPG]
    a = world.people.a
    world.open(screeners=(a,))
    for item in world.screening_queue(a):
        # The lone human includes the colitis trial, which the AI verifiably excludes.
        decision = Decision(outcome="include") if item.title.startswith(COLITIS) else None
        settle(world, a, item, decision or judged(item.title))

    (event,) = rescreen_events(world)
    assert (event["reason"], event["item_ref"]) == (
        "ai_disagreement",
        world.record(COLITIS).item_ref,
    )
    assert assert_only_human_disagreements(world) == []

    stream = world.screening_queue(world.people.auditor)
    assert sum(i.title.startswith(COLITIS) for i in stream) == 1
    assert len({frozenset(i.model_dump()) for i in stream}) == 1

    # Agreeing with the human here means including.
    overrides = {COLITIS: Decision(outcome="include")} if auditor_agrees else {}
    the_auditor_screens(world, overrides)
    with world.db.transaction() as tx:
        outcome = world.screening.outcomes(tx, world.project_id, SCOPE, STAGE)[
            world.record(COLITIS).item_ref
        ]
    if auditor_agrees:
        assert outcome == "include"
        assert assert_only_human_disagreements(world) == []
    else:
        assert outcome == "conflict"
        (title,) = assert_only_human_disagreements(world)
        assert title.startswith(COLITIS)
    with world.db.transaction() as tx:
        assert world.ledger.verify(tx, world.project_id).ok
