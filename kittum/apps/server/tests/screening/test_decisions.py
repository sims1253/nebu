"""Decisions as commands: replay, optimistic concurrency, undo, and full-text readings."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.screening import (
    CriterionDecision,
    Decision,
    InvalidDecision,
    NotAssigned,
    StaleRevision,
)
from tests.screening.support import SCOPE, STAGE, build_world, timing

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database, Tx
    from kittum.protocol import Stage
    from kittum.screening import DecideResult, QueueItem
    from tests.screening.support import World

pytestmark = pytest.mark.postgres


def item(world: World, reviewer: Id, prefix: str, stage: Stage = STAGE) -> QueueItem:
    with world.db.transaction() as tx:
        items = world.screening.queue(tx, world.project_id, reviewer, SCOPE, stage, limit=50)
    (found,) = [i for i in items if i.title.startswith(prefix)]
    return found


def decide(
    world: World,
    tx: Tx,
    reviewer: Id,
    assignment_id: Id,
    decision: Decision,
    revision: int,
    *,
    phase: str = "provisional",
    command_id: Id | None = None,
) -> DecideResult:
    return world.screening.decide(
        tx,
        command_id=command_id or new_id(),
        project_id=world.project_id,
        reviewer_id=reviewer,
        assignment_id=assignment_id,
        decision=decision,
        phase=phase,  # type: ignore[arg-type]
        expected_revision=revision,
        timing=timing(0),
    )


def counts(world: World) -> tuple[int, int]:
    with world.db.transaction() as tx:
        events = len(world.ledger.events(tx, world.project_id))
        row = tx.execute(
            "select count(*) as n from screening_decision d join screening_assignment a"
            " on a.id = d.assignment_id join screening_stage s on s.id = a.stage_id"
            " where s.project_id = %s",
            (world.project_id,),
        ).fetchone()
    assert row is not None
    return events, row["n"]


@pytest.fixture
def world(database: Database) -> World:
    world = build_world(database)
    keep = ("Nicotine patch versus", "Efficacy and safety of lumacaftor", "Nicotine gum")
    world.records = [world.record(t) for t in keep]
    world.open()
    return world


def test_replay_is_a_noop_stale_revisions_fail_and_undo_redecides(world: World) -> None:
    a, b = world.people.a, world.people.b
    patch = item(world, a, "Nicotine patch")
    include = Decision(outcome="include")
    command = new_id()
    with world.db.transaction() as tx:
        first = decide(world, tx, a, patch.assignment_id, include, 0, command_id=command)
    assert (first.phase, first.revision) == ("final", 1)
    before = counts(world)

    # A retry with the same command ID returns the stored result and writes nothing.
    with world.db.transaction() as tx:
        again = decide(world, tx, a, patch.assignment_id, include, 0, command_id=command)
    assert again == first
    assert counts(world) == before

    # Another reviewer cannot claim the command or the assignment.
    with world.db.transaction() as tx, pytest.raises(NotAssigned):
        decide(world, tx, b, patch.assignment_id, include, 1)

    # A stale revision is rejected, and nothing is written.
    with world.db.transaction() as tx, pytest.raises(StaleRevision):
        decide(
            world,
            tx,
            a,
            patch.assignment_id,
            Decision(outcome="exclude", reason_criterion_id="POP"),
            0,
        )
    assert counts(world) == before

    # Rules of the decision itself.
    gum = item(world, a, "Nicotine gum")
    with world.db.transaction() as tx:
        with pytest.raises(InvalidDecision, match="needs the criterion"):
            decide(world, tx, a, gum.assignment_id, Decision(outcome="exclude"), 0)
        with pytest.raises(InvalidDecision, match="not an outcome"):
            decide(world, tx, a, gum.assignment_id, Decision(outcome="insufficient_information"), 0)
        with pytest.raises(InvalidDecision, match="does not apply"):
            decide(
                world,
                tx,
                a,
                gum.assignment_id,
                Decision(outcome="exclude", reason_criterion_id="FOLLOWUP"),
                0,
            )
        with pytest.raises(InvalidDecision, match="follows a provisional"):
            decide(world, tx, a, gum.assignment_id, include, 0, phase="final")

    # Undo puts the record back in the queue with a new revision; the old decision stays.
    undo_command = new_id()
    with world.db.transaction() as tx:
        undone = world.screening.undo(
            tx,
            command_id=undo_command,
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=patch.assignment_id,
            expected_revision=1,
        )
        replayed = world.screening.undo(
            tx,
            command_id=undo_command,
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=patch.assignment_id,
            expected_revision=1,
        )
    assert undone.revision == 2 and replayed == undone
    back = item(world, a, "Nicotine patch")
    assert (back.revision, back.phase) == (2, "provisional")
    with world.db.transaction() as tx:
        redone = decide(world, tx, a, back.assignment_id, include, 2)
        assert redone.revision == 3
        with pytest.raises(InvalidDecision, match="no decision to undo"):
            world.screening.undo(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=a,
                assignment_id=gum.assignment_id,
                expected_revision=0,
            )
    assert counts(world)[1] == before[1] + 1  # the undone decision is kept

    # Undo withdraws an open conflict; re-deciding raises the conflict the new decisions call for.
    cf = "Efficacy and safety of lumacaftor"
    with world.db.transaction() as tx:
        decide(world, tx, a, item(world, a, cf).assignment_id, include, 0)
        b_cf = item(world, b, cf)
        b_first = decide(
            world,
            tx,
            b,
            b_cf.assignment_id,
            Decision(outcome="exclude", reason_criterion_id="POP"),
            0,
        )
        # The model answered not_met on the population too (the filter downgraded it), so it
        # agrees with B and nothing is revealed.
        assert (b_first.phase, b_first.reveal) == ("final", None)

    def open_conflicts() -> list[tuple[str, ...]]:
        with world.db.transaction() as tx:
            rows = tx.execute(
                "select kinds from screening_conflict c join screening_stage s on s.id = c.stage_id"
                " where s.project_id = %s and c.state = 'open'",
                (world.project_id,),
            ).fetchall()
        return [tuple(r["kinds"]) for r in rows]

    assert open_conflicts() == [("human_disagreement",)]
    a_cf = item_any(world, a, cf)
    with world.db.transaction() as tx:
        world.screening.undo(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=a_cf,
            expected_revision=1,
        )
    assert open_conflicts() == []
    # A now excludes too. The model's own reading agrees, so nothing is escalated.
    with world.db.transaction() as tx:
        result = decide(
            world, tx, a, a_cf, Decision(outcome="exclude", reason_criterion_id="POP"), 2
        )
    assert (result.phase, result.revision) == ("final", 3)
    assert open_conflicts() == []
    # A changes their mind again; the disagreement is back.
    with world.db.transaction() as tx:
        world.screening.undo(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=a_cf,
            expected_revision=3,
        )
        assert decide(world, tx, a, a_cf, include, 4).revision == 5
    assert open_conflicts() == [("human_disagreement",)]

    # Once adjudicated, a decision stands.
    with world.db.transaction() as tx:
        (conflict,) = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
        world.screening.adjudicate(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            adjudicator_id=world.people.adjudicator,
            conflict_id=conflict.conflict_id,
            expected_revision=conflict.revision,
            adopted="A",
        )
        with pytest.raises(InvalidDecision, match="adjudicated"):
            world.screening.undo(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=a,
                assignment_id=a_cf,
                expected_revision=5,
            )
        assert world.ledger.verify(tx, world.project_id).ok


def item_any(world: World, reviewer: Id, prefix: str) -> Id:
    """The reviewer's assignment for a record, decided or not."""
    with world.db.transaction() as tx:
        row = tx.execute(
            "select a.id from screening_assignment a join screening_item i"
            " on i.stage_id = a.stage_id and i.item_ref = a.item_ref"
            " join screening_stage s on s.id = a.stage_id"
            " where s.project_id = %s and a.reviewer_id = %s and i.title like %s",
            (world.project_id, reviewer, prefix + "%"),
        ).fetchone()
    assert row is not None
    return row["id"]


def test_full_text_decisions_carry_per_criterion_readings_and_anchors(database: Database) -> None:
    world = build_world(database)
    world.records = [world.record("Nicotine patch versus"), world.record("Nicotine gum")]
    world.open(stage="full_text")
    a, b = world.people.a, world.people.b
    criteria = [c.id for c in world.criteria("full_text")]
    assert criteria == ["POP", "INT", "COMP-CONTROL", "DESIGN", "FOLLOWUP"]
    anchor, other = new_id(), new_id()

    def readings(**statuses: str) -> tuple[CriterionDecision, ...]:
        return tuple(
            CriterionDecision(
                criterion_id=c,
                status=statuses.get(c.replace("-", "_"), "met"),  # type: ignore[arg-type]
                evidence_anchor_ids=(anchor,) if c != "FOLLOWUP" else (other,),
            )
            for c in criteria
        )

    patch_a = item(world, a, "Nicotine patch", "full_text")
    with world.db.transaction() as tx:
        with pytest.raises(InvalidDecision, match="not an outcome"):
            decide(world, tx, a, patch_a.assignment_id, Decision(outcome="unclear"), 0)
        with pytest.raises(InvalidDecision, match="every criterion"):
            decide(
                world,
                tx,
                a,
                patch_a.assignment_id,
                Decision(outcome="exclude", reason_criterion_id="FOLLOWUP"),
                0,
            )
        no_evidence = tuple(
            r.model_copy(update={"evidence_anchor_ids": ()}) if r.criterion_id == "FOLLOWUP" else r
            for r in readings(FOLLOWUP="not_met")
        )
        with pytest.raises(InvalidDecision, match="with evidence"):
            decide(
                world,
                tx,
                a,
                patch_a.assignment_id,
                Decision(outcome="exclude", reason_criterion_id="FOLLOWUP", criteria=no_evidence),
                0,
            )
        with pytest.raises(InvalidDecision, match="every criterion met"):
            decide(
                world,
                tx,
                a,
                patch_a.assignment_id,
                Decision(outcome="include", criteria=readings(DESIGN="unclear")),
                0,
            )

        # A excludes for lack of follow-up, with evidence; B includes with every criterion met.
        excluded = Decision(
            outcome="exclude", reason_criterion_id="FOLLOWUP", criteria=readings(FOLLOWUP="not_met")
        )
        first = decide(world, tx, a, patch_a.assignment_id, excluded, 0)
        if first.phase == "provisional":
            decide(world, tx, a, patch_a.assignment_id, excluded, first.revision, phase="final")
        included = Decision(outcome="include", criteria=readings())
        decide(
            world, tx, b, item(world, b, "Nicotine patch", "full_text").assignment_id, included, 0
        )

        # Insufficient information needs an unclear criterion; it counts as include.
        gum_a = item(world, a, "Nicotine gum", "full_text")
        with pytest.raises(InvalidDecision, match="unclear"):
            decide(
                world,
                tx,
                a,
                gum_a.assignment_id,
                Decision(outcome="insufficient_information", criteria=readings()),
                0,
            )
        insufficient = Decision(
            outcome="insufficient_information",
            criteria=readings(FOLLOWUP="unclear"),
            comment="Author query sent.",
        )
        assert decide(world, tx, a, gum_a.assignment_id, insufficient, 0).phase == "final"

        # The adjudicator sees each reviewer's readings and evidence anchors.
        (conflict,) = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, "full_text"
        )
        columns = {c.outcome: c for c in conflict.columns}
        assert columns["exclude"].reason_criterion_id == "FOLLOWUP"
        followup = next(r for r in columns["exclude"].criteria if r.criterion_id == "FOLLOWUP")
        assert followup.status == "not_met" and followup.evidence_anchor_ids == (other,)
        assert {r.status for r in columns["include"].criteria} == {"met"}
        assert world.ledger.verify(tx, world.project_id).ok
