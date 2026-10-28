"""Sentinels and audit samples: invisible to reviewers, counted in the audit gauge."""

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.core import new_id
from kittum.screening import STAGE_OPENED, Decision
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.screening.support import (
    NON_SMOKERS,
    RELEVANT,
    SCOPE,
    STAGE,
    build_world,
    recorded_backend,
    simulated_answers,
    timing,
)

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.core import Id
    from kittum.db import Database
    from kittum.screening import QueueItem
    from tests.screening.support import World

pytestmark = pytest.mark.postgres

HIDDEN = ("sentinel", "audit", "expected_outcome", "kind", "excludes", "rank")


def settle(world: World, reviewer: Id, item: QueueItem, outcome: str) -> None:
    """Decide, keeping the decision through a reveal."""
    decision = Decision(
        outcome=outcome,  # type: ignore[arg-type]
        reason_criterion_id="POP" if outcome == "exclude" else None,
    )
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


def test_sentinels_and_audits_are_invisible_but_count_in_the_gauge(
    database: Database, tmp_path: Path
) -> None:
    # M2 on 40 simulated records: 36 the AI excludes on a verified quote, 4 relevant ones.
    world = build_world(database, "M2", simulate={NON_SMOKERS: 36, RELEVANT: 4})
    a, auditor = world.people.a, world.people.auditor
    backend = recorded_backend(
        world.records, world.criteria(), answers=simulated_answers(world.records)
    )
    world.open(screeners=(a,), auditor=auditor, backend=backend)

    # The ledger names the draws; reviewers never learn them.
    with world.db.transaction() as tx:
        (opened,) = [e for e in world.ledger.events(tx, world.project_id) if e.type == STAGE_OPENED]
    audited = set(opened.payload["audit_refs"])
    assert len(audited) == 4  # 10% of 40
    assert opened.payload["sentinel_refs"] == ["sentinel:nrt-lozenge"]  # 1% of 40, rounded up

    settings = Settings(database_url="postgresql://unused", artifact_dir=tmp_path)
    context = AppContext(
        settings=settings, db=world.db, artifacts=MemoryStore(), ledger=world.ledger
    )
    with TestClient(create_app(context)) as client:

        def get(reviewer: Id, path: str) -> tuple[str, object]:
            response = client.get(
                f"/api/projects/{world.project_id}/screening/{path}",
                params={"scope_id": SCOPE, "stage": STAGE, "limit": 50},
                headers={"X-Kittum-Reviewer": str(reviewer)},
            )
            assert response.status_code == 200
            return response.text, response.json()

        # The screener's queue: 40 records and the sentinel, all alike.
        text, items = get(a, "queue")
        assert isinstance(items, list) and len(items) == 41
        assert len({frozenset(i) for i in items}) == 1
        assert not any(word in text for word in HIDDEN)
        # Their own counts include the hidden items, so nothing stands out.
        _, own = get(a, "status")
        assert own == {
            "scope_id": SCOPE,
            "stage": STAGE,
            "view": "reviewer",
            "screened": 0,
            "remaining": 41,
            "conflicts": None,
            "metrics": None,
        }
        # The auditor's queue: the four audit samples and the sentinel, nothing marked.
        text, items = get(auditor, "queue")
        assert isinstance(items, list) and len(items) == 5
        assert not any(word in text for word in HIDDEN)

    # Below the service: the reviewer's SQL function has no column that could leak.
    with world.db.transaction() as tx:
        cur = tx.execute(
            "select * from screening_reviewer_queue(%s, %s, %s, %s) limit 0",
            (world.project_id, a, SCOPE, STAGE),
        )
        assert cur.description is not None
        assert {c.name for c in cur.description} == {
            "assignment_id",
            "stage",
            "scope_id",
            "title",
            "text",
            "facts",
            "revision",
            "state",
            "own_reveal",
            "position",
        }
        refs = {
            r["assignment_id"]: r["item_ref"]
            for r in tx.execute(
                "select id as assignment_id, item_ref from screening_assignment"
                " where reviewer_id = %s",
                (auditor,),
            ).fetchall()
        }

    # The screener excludes what the AI excludes and includes the rest, but misses the
    # sentinel (a known include).
    for item in world.screening_queue(a):
        relevant = item.title.startswith(RELEVANT)
        settle(world, a, item, "include" if relevant else "exclude")
    # The auditor finds one relevant record among the audited exclusions and gets the
    # sentinel right.
    audit_items = world.screening_queue(auditor)
    excluded_audits = sorted(
        (
            i
            for i in audit_items
            if refs[i.assignment_id] in audited and i.title.startswith(NON_SMOKERS)
        ),
        key=lambda i: i.title,
    )
    assert excluded_audits
    miss = excluded_audits[0]
    for item in audit_items:
        found = item == miss or item.title.startswith(("Nicotine", RELEVANT))
        settle(world, auditor, item, "include" if found else "exclude")

    with world.db.transaction() as tx:
        status = world.screening.status(tx, world.project_id, world.people.lead, SCOPE, STAGE)
        assert status.metrics is not None
        gauge = status.metrics.audit
        assert (gauge.sentinels, gauge.sentinels_decided, gauge.sentinels_correct) == (2, 2, 1)
        assert gauge.audit_samples == 4 and gauge.audit_decided == 4
        # The excluded population: the 36 records excluded by the human and the AI alike.
        assert gauge.excluded_population == 36
        assert gauge.audit_relevant_found == 1
        sampled = len(excluded_audits)
        assert gauge.elusion is not None
        assert gauge.elusion.estimate == pytest.approx(36 / sampled)
        assert gauge.elusion.upper > gauge.elusion.estimate
        # The found miss goes to adjudication; sentinels never make conflicts.
        conflicts = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
        assert [c.title for c in conflicts] == [miss.title]
        assert sorted(c.outcome for c in conflicts[0].columns) == ["exclude", "include"]
        assert world.ledger.verify(tx, world.project_id).ok
