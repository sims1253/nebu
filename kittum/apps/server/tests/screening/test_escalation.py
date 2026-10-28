"""When the AI's disagreement escalates a record: the model's own reading argues for keeping a
record, the verified reading for removing one.

The judge's filter turns an unverified `not_met` into `unclear`, which keeps the record. That
protects against wrongful AI exclusions; it must not make the AI "disagree" with humans who
excluded a record the model itself read as failing a criterion.
"""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.screening import Decision
from tests.screening.support import SCOPE, STAGE, build_world, timing

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database
    from kittum.screening import DecideResult, ScopeStatus
    from tests.screening.support import World

pytestmark = pytest.mark.postgres

DOWNGRADED = "Varenicline versus placebo"  # the model: INT not_met; the filter: unclear
DOWNGRADED_TOO = "Use of nicotine replacement"  # the model: DESIGN not_met; the filter: unclear
NO_FAILING = "GLPG2737 in lumacaftor"  # the model: every criterion met


def setup(database: Database, mode: str, titles: tuple[str, ...]) -> World:
    world = build_world(database, mode)
    world.records = [world.record(t) for t in titles]
    if mode == "M1":
        world.open()
    else:
        world.open(screeners=(world.people.a,), auditor=world.people.auditor)
    return world


def screen(world: World, reviewer: Id, title: str, decision: Decision) -> DecideResult:
    """A provisional decision, kept through a reveal."""
    with world.db.transaction() as tx:
        items = world.screening.queue(tx, world.project_id, reviewer, SCOPE, STAGE, limit=50)
        (item,) = [i for i in items if i.title.startswith(title)]
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


def finish(world: World) -> None:
    """Every remaining assignment decided: sentinels included, records excluded."""
    reviewers = (world.people.a, world.people.b, world.people.auditor)
    for reviewer in reviewers:
        with world.db.transaction() as tx:
            items = world.screening.queue(tx, world.project_id, reviewer, SCOPE, STAGE, limit=50)
        for item in items:
            sentinel = item.title.startswith("Nicotine")
            decision = (
                Decision(outcome="include")
                if sentinel
                else Decision(outcome="exclude", reason_criterion_id="POP")
            )
            screen(world, reviewer, item.title, decision)


def model_said(world: World, title: str) -> dict[str, tuple[str, str | None]]:
    """Per criterion: (status after the filter, what the model said if it was downgraded)."""
    with world.db.transaction() as tx:
        row = tx.execute(
            "select r.excludes, r.criteria from screening_ai_reading r join screening_item i"
            " on i.stage_id = r.stage_id and i.item_ref = r.item_ref"
            " join screening_stage s on s.id = r.stage_id"
            " where s.project_id = %s and i.title like %s",
            (world.project_id, title + "%"),
        ).fetchone()
    assert row is not None and row["excludes"] is False
    return {c["criterion_id"]: (c["status"], c["downgraded_from"]) for c in row["criteria"]}


def status(world: World) -> ScopeStatus:
    with world.db.transaction() as tx:
        return world.screening.status(tx, world.project_id, world.people.lead, SCOPE, STAGE)


def adjudications(world: World) -> list[str]:
    with world.db.transaction() as tx:
        queue = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
    return [c.title for c in queue]


def test_m1_downgraded_not_met_with_both_humans_excluding_is_not_adjudicated(
    database: Database,
) -> None:
    world = setup(database, "M1", (DOWNGRADED,))
    assert model_said(world, DOWNGRADED)["INT"] == ("unclear", "not_met")
    exclusion = Decision(outcome="exclude", reason_criterion_id="INT")
    for reviewer in (world.people.a, world.people.b):
        # The model agrees with the exclusion, so there is nothing to reveal.
        assert screen(world, reviewer, DOWNGRADED, exclusion).reveal is None
    assert adjudications(world) == []
    now = status(world)
    assert (now.screened, now.conflicts) == (1, 0)
    # The record stays in the audit's sampling frame, among records the AI did not verifiably
    # exclude.
    assert now.metrics is not None
    assert now.metrics.audit.excluded_population == 1
    (stratum,) = [s for s in now.metrics.audit.strata if s.population]
    assert stratum.ai_excludes is False


def test_m1_model_seeing_no_failing_criterion_with_both_humans_excluding_is_adjudicated(
    database: Database,
) -> None:
    world = setup(database, "M1", (NO_FAILING,))
    assert {s for s, _ in model_said(world, NO_FAILING).values()} == {"met"}
    exclusion = Decision(outcome="exclude", reason_criterion_id="POP")
    for reviewer in (world.people.a, world.people.b):
        result = screen(world, reviewer, NO_FAILING, exclusion)
        assert result.reveal is not None and result.reveal.status == "met"
    # Escalated, but held back: no agreed record is free to cover it yet.
    now = status(world)
    assert now.conflicts == 1 and now.metrics is not None and now.metrics.decoys.held == 1
    assert now.metrics.audit.excluded_population == 0
    assert adjudications(world) == []
    # Once screening ends without cover, it goes to the adjudicator, flagged for the lead.
    finish(world)
    (title,) = adjudications(world)
    assert title.startswith(NO_FAILING)
    assert status(world).metrics.decoys.released_without_cover == 1  # type: ignore[union-attr]


def test_m2_downgraded_not_met_is_agreement_either_way(database: Database) -> None:
    world = setup(database, "M2", (DOWNGRADED, DOWNGRADED_TOO))
    assert model_said(world, DOWNGRADED_TOO)["DESIGN"] == ("unclear", "not_met")
    a = world.people.a
    # The human excludes: the model's own reading agrees.
    exclusion = Decision(outcome="exclude", reason_criterion_id="INT")
    assert screen(world, a, DOWNGRADED, exclusion).reveal is None
    # The human includes: an exclusion the filter could not verify is not revealed, and agrees
    # with the inclusion.
    assert screen(world, a, DOWNGRADED_TOO, Decision(outcome="include")).reveal is None
    assert adjudications(world) == []
    assert status(world).conflicts == 0


def test_m2_model_including_against_a_human_exclusion_is_adjudicated(database: Database) -> None:
    world = setup(database, "M2", (NO_FAILING,))
    result = screen(
        world, world.people.a, NO_FAILING, Decision(outcome="exclude", reason_criterion_id="POP")
    )
    assert result.reveal is not None and result.reveal.criterion_id == "POP"
    assert adjudications(world) == []  # held until cover, or the end of screening
    finish(world)
    (title,) = adjudications(world)
    assert title.startswith(NO_FAILING)
    with world.db.transaction() as tx:
        kinds = tx.execute(
            "select c.kinds from screening_conflict c join screening_stage s"
            " on s.id = c.stage_id where s.project_id = %s",
            (world.project_id,),
        ).fetchone()
        assert kinds == {"kinds": ["ai_disagreement"]}
        assert world.ledger.verify(tx, world.project_id).ok
