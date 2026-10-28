"""Decoys: the adjudication queue must not reveal the AI before the adjudicator commits."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.screening import (
    CONFLICT_OPENED,
    CONFLICT_PUBLISHED,
    RELEASED_WITHOUT_COVER,
    AdjudicationItem,
    Decision,
)
from tests.screening.support import SCOPE, STAGE, build_world, timing

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database
    from kittum.screening import QueueItem, ScopeStatus
    from tests.screening.support import World

pytestmark = pytest.mark.postgres

COLITIS = "Transdermal nicotine for active"  # the AI's exclusion is verified
PATCH = "Nicotine patch versus"  # the AI includes


def settle(world: World, reviewer: Id, item: QueueItem, decision: Decision) -> None:
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
    assert (result.phase, result.reveal) == ("final", None)  # the stage never reveals


def item(world: World, reviewer: Id, title: str) -> QueueItem:
    (found,) = [i for i in world.screening_queue(reviewer) if i.title.startswith(title)]
    return found


def adjudications(world: World, adjudicator: Id) -> list[AdjudicationItem]:
    with world.db.transaction() as tx:
        return world.screening.adjudication_queue(tx, world.project_id, adjudicator, SCOPE, STAGE)


def status(world: World, viewer: Id) -> ScopeStatus:
    with world.db.transaction() as tx:
        return world.screening.status(tx, world.project_id, viewer, SCOPE, STAGE)


def test_m2_decoys_hide_which_inclusions_the_ai_contested(database: Database) -> None:
    world = build_world(database, "M2")
    world.records = [world.record(t) for t in (COLITIS, PATCH)]
    a, adjudicator, lead = world.people.a, world.people.adjudicator, world.people.lead
    # The reviewer never sees the AI; a lone human inclusion in the adjudication queue would be
    # there only because the AI excluded it.
    world.open(screeners=(a,), auditor=world.people.auditor, reveal="never")
    include = Decision(outcome="include")

    # The contested record first. No agreed record can cover it yet, so it stays invisible to
    # the adjudicator, and so does any count of it.
    settle(world, a, item(world, a, COLITIS), include)
    assert adjudications(world, adjudicator) == []
    assert (status(world, adjudicator).view, status(world, adjudicator).conflicts) == (
        "adjudicator",
        0,
    )
    held = status(world, lead)
    assert held.conflicts == 1 and held.metrics is not None and held.metrics.decoys.held == 1

    # The patch trial completes with the same visible outcome and becomes the decoy; both
    # appear in the same transaction.
    settle(world, a, item(world, a, PATCH), include)
    queue = adjudications(world, adjudicator)
    assert len(queue) == 2
    genuine, decoy = sorted(queue, key=lambda c: not c.title.startswith(COLITIS))
    assert decoy.title.startswith(PATCH)
    with world.db.transaction() as tx:
        events = world.ledger.events(tx, world.project_id)
    published = next(e for e in events if e.type == CONFLICT_PUBLISHED)
    opened = next(e for e in events if e.type == CONFLICT_OPENED and e.payload["decoy"])
    assert published.payload["conflict_id"] == str(genuine.conflict_id)
    assert opened.payload["partner_id"] == str(genuine.conflict_id)
    assert abs(published.seq - opened.seq) == 1  # one command, one transaction

    # Indistinguishable: the same fields and revision, one column with the same decision, and
    # random IDs whose order says nothing about which conflict came first.
    def tells(item: AdjudicationItem) -> dict[str, object]:
        dumped = item.model_dump(exclude={"conflict_id", "title", "text", "record"})
        return {**dumped, "record_fields": sorted(item.record.model_dump())}

    assert tells(genuine) == tells(decoy)
    assert genuine.columns[0].outcome == decoy.columns[0].outcome == "include"
    assert genuine.conflict_id.version == decoy.conflict_id.version == 4

    # The adjudicator's status: one combined count, no split, no metrics. The lead sees both.
    mine = status(world, adjudicator)
    assert (mine.view, mine.conflicts, mine.metrics) == ("adjudicator", 2, None)
    split = status(world, lead)
    assert split.view == "lead" and split.conflicts == 1 and split.metrics is not None
    assert (split.metrics.decoys.cases, split.metrics.decoys.held) == (1, 0)

    # Adjudicating the decoy records a decision but changes no outcome; the genuine one does.
    exclusion = Decision(outcome="exclude", reason_criterion_id="POP")
    with world.db.transaction() as tx:
        for conflict in (decoy, genuine):
            result = world.screening.adjudicate(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                adjudicator_id=adjudicator,
                conflict_id=conflict.conflict_id,
                expected_revision=conflict.revision,
                decision=exclusion,
                timing=timing(0),
            )
            assert result.outcome == "exclude"
        outcomes = world.screening.outcomes(tx, world.project_id, SCOPE, STAGE)
        by_title = {prefix: outcomes[world.record(prefix).item_ref] for prefix in (PATCH, COLITIS)}
        assert by_title == {PATCH: "include", COLITIS: "exclude"}
        after = world.screening.status(tx, world.project_id, lead, SCOPE, STAGE)
        assert after.conflicts == 0 and after.metrics is not None
        assert after.metrics.conflicts_resolved == 1
        assert after.metrics.decoys.model_dump() == {
            "cases": 1,
            "adjudicated": 1,
            "matching": 0,
            "held": 0,
            "released_without_cover": 0,
        }
        assert world.ledger.verify(tx, world.project_id).ok


def test_uncovered_ai_conflict_is_released_to_one_adjudicator_when_screening_ends(
    database: Database,
) -> None:
    world = build_world(database, "M2")
    world.records = [world.record(COLITIS)]
    a, auditor, adjudicator = world.people.a, world.people.auditor, world.people.adjudicator
    # The auditor also adjudicates, but reviews the only record, so the release goes elsewhere.
    world.open(screeners=(a,), auditor=auditor, adjudicators=(auditor, adjudicator), reveal="never")
    include = Decision(outcome="include")

    settle(world, a, item(world, a, COLITIS), include)
    settle(world, a, item(world, a, "Nicotine lozenge"), include)  # the sentinel
    settle(world, auditor, item(world, auditor, COLITIS), include)
    assert adjudications(world, adjudicator) == []  # one assignment is still open

    # The last assignment: no decoy can come any more, so the conflict is released.
    settle(world, auditor, item(world, auditor, "Nicotine lozenge"), include)
    (released,) = adjudications(world, adjudicator)
    assert released.title.startswith(COLITIS) and released.conflict_id.version == 4
    assert adjudications(world, auditor) == []
    # The adjudicator's item and status look like any other: no flag, no AI.
    assert set(released.model_dump()) == set(AdjudicationItem.model_fields)
    mine = status(world, adjudicator)
    assert (mine.view, mine.conflicts, mine.metrics) == ("adjudicator", 1, None)

    # The lead's view and the ledger carry the flag, for the AI disclosure.
    lead = status(world, world.people.lead)
    assert lead.metrics is not None
    assert (lead.metrics.decoys.held, lead.metrics.decoys.released_without_cover) == (0, 1)
    with world.db.transaction() as tx:
        (event,) = [
            e for e in world.ledger.events(tx, world.project_id) if e.type == RELEASED_WITHOUT_COVER
        ]
        assert event.payload["conflict_id"] == str(released.conflict_id)
        assert event.payload["assigned_to"] == str(adjudicator)
        result = world.screening.adjudicate(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            adjudicator_id=adjudicator,
            conflict_id=released.conflict_id,
            expected_revision=released.revision,
            adopted="A",
        )
        assert result.ai.excludes is True  # the AI column opens only now
        assert world.ledger.verify(tx, world.project_id).ok
