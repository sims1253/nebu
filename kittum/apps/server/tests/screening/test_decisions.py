"""Decisions as commands: replay, optimistic concurrency, undo, and full-text readings."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id, sha256_hex
from kittum.screening import (
    AdjudicationItem,
    CriterionDecision,
    DecideResult,
    Decision,
    InvalidDecision,
    NotAssigned,
    QueueItem,
    StaleRevision,
    UndoResult,
)
from tests.screening.support import SCOPE, STAGE, build_world, timing

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database, Tx
    from kittum.protocol import Stage
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
    etag: str,
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
        expected_etag=etag,
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
        first = decide(world, tx, a, patch.assignment_id, include, patch.etag, command_id=command)
    assert first.phase == "final" and first.etag != patch.etag
    before = counts(world)

    # A retry with the same command ID returns the stored result and writes nothing.
    with world.db.transaction() as tx:
        again = decide(world, tx, a, patch.assignment_id, include, patch.etag, command_id=command)
    assert again == first
    assert counts(world) == before

    # Another reviewer cannot claim the command or the assignment.
    with world.db.transaction() as tx, pytest.raises(NotAssigned):
        decide(world, tx, b, patch.assignment_id, include, first.etag)

    # A stale token is rejected, and nothing is written.
    exclusion = Decision(outcome="exclude", reason_criterion_id="POP")
    with world.db.transaction() as tx, pytest.raises(StaleRevision):
        decide(world, tx, a, patch.assignment_id, exclusion, patch.etag)
    assert counts(world) == before

    # Rules of the decision itself.
    gum = item(world, a, "Nicotine gum")
    with world.db.transaction() as tx:
        with pytest.raises(InvalidDecision, match="needs the criterion"):
            decide(world, tx, a, gum.assignment_id, Decision(outcome="exclude"), gum.etag)
        with pytest.raises(InvalidDecision, match="not an outcome"):
            decide(
                world,
                tx,
                a,
                gum.assignment_id,
                Decision(outcome="insufficient_information"),
                gum.etag,
            )
        with pytest.raises(InvalidDecision, match="does not apply"):
            decide(
                world,
                tx,
                a,
                gum.assignment_id,
                Decision(outcome="exclude", reason_criterion_id="FOLLOWUP"),
                gum.etag,
            )
        with pytest.raises(InvalidDecision, match="only after a provisional"):
            decide(world, tx, a, gum.assignment_id, include, gum.etag, phase="final")

    # Undo puts the record back in the queue under a new token; the old decision stays.
    undo_command = new_id()
    with world.db.transaction() as tx:
        undone = world.screening.undo(
            tx,
            command_id=undo_command,
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=patch.assignment_id,
            expected_etag=first.etag,
        )
        replayed = world.screening.undo(
            tx,
            command_id=undo_command,
            project_id=world.project_id,
            reviewer_id=a,
            assignment_id=patch.assignment_id,
            expected_etag=first.etag,
        )
    assert replayed == undone and undone.etag not in (patch.etag, first.etag)
    back = item(world, a, "Nicotine patch")
    assert (back.etag, back.phase) == (undone.etag, "provisional")
    with world.db.transaction() as tx:
        redone = decide(world, tx, a, back.assignment_id, include, back.etag)
        assert redone.etag not in (patch.etag, first.etag, undone.etag)
        with pytest.raises(InvalidDecision, match="no decision to undo"):
            world.screening.undo(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=a,
                assignment_id=gum.assignment_id,
                expected_etag=gum.etag,
            )
    assert counts(world)[1] == before[1] + 1  # the undone decision is kept

    # Undo withdraws an open conflict; re-deciding raises the conflict the new decisions call for.
    cf = "Efficacy and safety of lumacaftor"
    with world.db.transaction() as tx:
        a_item = item(world, a, cf)
        decide(world, tx, a, a_item.assignment_id, include, a_item.etag)
        b_cf = item(world, b, cf)
        b_first = decide(world, tx, b, b_cf.assignment_id, exclusion, b_cf.etag)
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

    def undo(assignment_id: Id, etag: str) -> str:
        with world.db.transaction() as tx:
            return world.screening.undo(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=a,
                assignment_id=assignment_id,
                expected_etag=etag,
            ).etag

    assert open_conflicts() == [("human_disagreement",)]
    a_cf, etag = item_any(world, a, cf)
    etag = undo(a_cf, etag)
    assert open_conflicts() == []
    # A now excludes too. The model's own reading agrees, so nothing is escalated.
    with world.db.transaction() as tx:
        result = decide(world, tx, a, a_cf, exclusion, etag)
    assert result.phase == "final"
    assert open_conflicts() == []
    # A changes their mind again; the disagreement is back.
    etag = undo(a_cf, result.etag)
    with world.db.transaction() as tx:
        etag = decide(world, tx, a, a_cf, include, etag).etag
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
            expected_etag=conflict.etag,
            adopted="A",
        )
        with pytest.raises(InvalidDecision, match="adjudicated"):
            world.screening.undo(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=a,
                assignment_id=a_cf,
                expected_etag=etag,
            )
        assert world.ledger.verify(tx, world.project_id).ok


def test_tokens_are_opaque_and_reveal_no_decision_counts(world: World) -> None:
    """Clients get a random token per state, never a revision count."""
    a = world.people.a
    for shape in (QueueItem, DecideResult, UndoResult, AdjudicationItem):
        assert "revision" not in shape.model_fields and "etag" in shape.model_fields
    patch, gum = item(world, a, "Nicotine patch"), item(world, a, "Nicotine gum")
    seen = {patch.etag, gum.etag}
    assert len(seen) == 2  # two untouched items share no token
    etag = patch.etag
    for _ in range(2):  # decide, undo, decide, undo: each state its own token
        with world.db.transaction() as tx:
            etag = decide(world, tx, a, patch.assignment_id, Decision(outcome="include"), etag).etag
            seen.add(etag)
            etag = world.screening.undo(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=a,
                assignment_id=patch.assignment_id,
                expected_etag=etag,
            ).etag
            seen.add(etag)
    assert len(seen) == 6
    # The token is random, not a function of the assignment and a counter.
    assert all(len(t) == 32 and int(t, 16) >= 0 for t in seen)
    counted = {sha256_hex(f"{patch.assignment_id}{n}".encode())[:32] for n in range(10)} | {
        sha256_hex(f"{patch.assignment_id}:{n}".encode())[:32] for n in range(10)
    }
    assert not seen & counted


def item_any(world: World, reviewer: Id, prefix: str) -> tuple[Id, str]:
    """The reviewer's assignment for a record, decided or not, and its token."""
    with world.db.transaction() as tx:
        row = tx.execute(
            "select a.id, a.etag from screening_assignment a join screening_item i"
            " on i.stage_id = a.stage_id and i.item_ref = a.item_ref"
            " join screening_stage s on s.id = a.stage_id"
            " where s.project_id = %s and a.reviewer_id = %s and i.title like %s",
            (world.project_id, reviewer, prefix + "%"),
        ).fetchone()
    assert row is not None
    return row["id"], row["etag"]


def test_full_text_decisions_need_one_reason_not_a_verdict_per_criterion(
    database: Database,
) -> None:
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
    patch_b = item(world, b, "Nicotine patch", "full_text")
    with world.db.transaction() as tx:
        with pytest.raises(InvalidDecision, match="not an outcome"):
            decide(world, tx, a, patch_a.assignment_id, Decision(outcome="unclear"), patch_a.etag)
        # An exclusion cites the failing criterion, read not met, with evidence for it.
        for reasons in (
            (),
            (CriterionDecision(criterion_id="FOLLOWUP", status="not_met"),),
            (
                CriterionDecision(
                    criterion_id="POP", status="not_met", evidence_anchor_ids=(anchor,)
                ),
            ),
        ):
            with pytest.raises(InvalidDecision, match="mark the failing criterion not met"):
                decide(
                    world,
                    tx,
                    a,
                    patch_a.assignment_id,
                    Decision(outcome="exclude", reason_criterion_id="FOLLOWUP", criteria=reasons),
                    patch_a.etag,
                )
        # An inclusion means every criterion is met: a reading that says otherwise is refused.
        with pytest.raises(InvalidDecision, match="every criterion is met"):
            decide(
                world,
                tx,
                a,
                patch_a.assignment_id,
                Decision(
                    outcome="include",
                    criteria=(CriterionDecision(criterion_id="DESIGN", status="unclear"),),
                ),
                patch_a.etag,
            )

        # A excludes for lack of follow-up, on that criterion alone. B includes in one action:
        # no verdict on each criterion and no evidence.
        excluded = Decision(
            outcome="exclude",
            reason_criterion_id="FOLLOWUP",
            evidence_anchor_ids=(other,),
            criteria=(
                CriterionDecision(
                    criterion_id="FOLLOWUP", status="not_met", evidence_anchor_ids=(other,)
                ),
            ),
        )
        first = decide(world, tx, a, patch_a.assignment_id, excluded, patch_a.etag)
        if first.phase == "provisional":
            decide(world, tx, a, patch_a.assignment_id, excluded, first.etag, phase="final")
        included = Decision(outcome="include")
        first = decide(world, tx, b, patch_b.assignment_id, included, patch_b.etag)
        if first.phase == "provisional":
            decide(world, tx, b, patch_b.assignment_id, included, first.etag, phase="final")

        # Insufficient information needs an unclear criterion; it counts as include.
        gum_a = item(world, a, "Nicotine gum", "full_text")
        with pytest.raises(InvalidDecision, match="unclear"):
            decide(
                world,
                tx,
                a,
                gum_a.assignment_id,
                Decision(outcome="insufficient_information", criteria=readings()),
                gum_a.etag,
            )
        # Like every full-text outcome, it cites evidence.
        unanchored = tuple(
            r.model_copy(update={"evidence_anchor_ids": ()}) for r in readings(FOLLOWUP="unclear")
        )
        with pytest.raises(InvalidDecision, match="at least one evidence anchor"):
            decide(
                world,
                tx,
                a,
                gum_a.assignment_id,
                Decision(outcome="insufficient_information", criteria=unanchored),
                gum_a.etag,
            )
        insufficient = Decision(
            outcome="insufficient_information",
            criteria=readings(FOLLOWUP="unclear"),
            comment="Author query sent.",
        )
        assert decide(world, tx, a, gum_a.assignment_id, insufficient, gum_a.etag).phase == "final"

        # The adjudicator sees each reviewer's reason and evidence, and adopts the inclusion.
        (conflict,) = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, "full_text"
        )
        columns = {c.outcome: c for c in conflict.columns}
        assert columns["exclude"].reason_criterion_id == "FOLLOWUP"
        (followup,) = columns["exclude"].criteria
        assert followup.status == "not_met" and followup.evidence_anchor_ids == (other,)
        assert columns["include"].criteria == ()
        world.screening.adjudicate(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            adjudicator_id=world.people.adjudicator,
            conflict_id=conflict.conflict_id,
            expected_etag=conflict.etag,
            adopted=columns["include"].label,
        )
        assert (
            world.screening.outcomes(tx, world.project_id, SCOPE, "full_text")[
                world.record("Nicotine patch").item_ref
            ]
            == "include"
        )
        # The lead's figures read the outcomes alone: kappa over the two decided records.
        status = world.screening.status(tx, world.project_id, world.people.lead, SCOPE, "full_text")
        assert status.metrics is not None
        (human,) = [p for p in status.metrics.agreement if p.kind == "human_human"]
        assert human.items == 1
        assert world.ledger.verify(tx, world.project_id).ok
