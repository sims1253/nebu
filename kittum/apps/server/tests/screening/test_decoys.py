"""Decoys: the adjudication queue must not reveal the AI before the adjudicator commits."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.screening import Decision
from tests.screening.support import SCOPE, STAGE, build_world, timing

if TYPE_CHECKING:
    from kittum.db import Database

pytestmark = pytest.mark.postgres

COLITIS = "Transdermal nicotine for active"  # the AI's exclusion is verified
PATCH = "Nicotine patch versus"  # the AI includes


def test_m2_decoys_hide_which_inclusions_the_ai_contested(database: Database) -> None:
    world = build_world(database, "M2")
    world.records = [world.record(t) for t in (COLITIS, PATCH)]
    a = world.people.a
    # The reviewer never sees the AI; a lone human inclusion in the adjudication queue would be
    # there only because the AI excluded it.
    world.open(screeners=(a,), auditor=world.people.auditor, reveal="never")

    include = Decision(outcome="include")
    for title in (COLITIS, PATCH):  # the contested record first: no decoy is free yet
        with world.db.transaction() as tx:
            items = world.screening.queue(tx, world.project_id, a, SCOPE, STAGE, limit=50)
            (item,) = [i for i in items if i.title.startswith(title)]
            result = world.screening.decide(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                reviewer_id=a,
                assignment_id=item.assignment_id,
                decision=include,
                phase="provisional",
                expected_revision=0,
                timing=timing(0),
            )
            assert (result.phase, result.reveal) == ("final", None)

    with world.db.transaction() as tx:
        queue = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
    assert sorted(c.title.startswith(COLITIS) for c in queue) == [False, True]
    assert sum(c.title.startswith(PATCH) for c in queue) == 1
    genuine, decoy = sorted(queue, key=lambda c: not c.title.startswith(COLITIS))

    # Indistinguishable: the same fields, the same revision, one column with the same decision.
    def tells(item: object) -> dict[str, object]:
        dumped = item.model_dump(exclude={"conflict_id", "title", "text", "record"})  # type: ignore[attr-defined]
        return {**dumped, "record_fields": sorted(item.record.model_dump())}  # type: ignore[attr-defined]

    assert tells(genuine) == tells(decoy)
    assert genuine.columns[0].outcome == decoy.columns[0].outcome == "include"

    # Adjudicating the decoy records a decision but changes no outcome; the genuine one does.
    exclusion = Decision(outcome="exclude", reason_criterion_id="POP")
    with world.db.transaction() as tx:
        for conflict in (decoy, genuine):
            result = world.screening.adjudicate(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                adjudicator_id=world.people.adjudicator,
                conflict_id=conflict.conflict_id,
                expected_revision=conflict.revision,
                decision=exclusion,
                timing=timing(0),
            )
            assert result.outcome == "exclude"
        outcomes = world.screening.outcomes(tx, world.project_id, SCOPE, STAGE)
        by_title = {prefix: outcomes[world.record(prefix).item_ref] for prefix in (PATCH, COLITIS)}
        assert by_title == {PATCH: "include", COLITIS: "exclude"}
        status = world.screening.status(tx, world.project_id, world.people.lead, SCOPE, STAGE)
        assert status.conflicts == 0 and status.metrics is not None
        assert status.metrics.conflicts_resolved == 1
        assert status.metrics.decoys.model_dump() == {"cases": 1, "adjudicated": 1, "matching": 0}
        assert world.ledger.verify(tx, world.project_id).ok
