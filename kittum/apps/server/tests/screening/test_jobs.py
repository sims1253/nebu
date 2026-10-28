"""Judging a stage as jobs: the worker runs the judge once per item, however often it is asked."""

from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.judge import VERDICT_RECORDED
from kittum.runs import PostgresJobQueue, PostgresReceipts
from kittum.screening import AI_READ, JUDGE_JOB, Screening, StageSpec, judge_handler
from tests.screening.support import (
    CONFIG,
    SCOPE,
    SENTINELS,
    STAGE,
    build_world,
    recorded_backend,
)

if TYPE_CHECKING:
    from kittum.db import Database
    from tests.screening.support import World


pytestmark = pytest.mark.postgres


def tally(world: World, stage_id: object) -> dict[str, int]:
    with world.db.transaction() as tx:
        events = world.ledger.events(tx, world.project_id)
        jobs = tx.execute(
            "select status, count(*) as n from job where kind = %s and payload->>'stage_id' = %s"
            " group by status",
            (JUDGE_JOB, str(stage_id)),
        ).fetchall()
        readings = tx.execute(
            "select count(*) as n from screening_ai_reading where stage_id = %s", (stage_id,)
        ).fetchone()
        verdicts = tx.execute(
            "select count(*) as n from verdict where project_id = %s", (world.project_id,)
        ).fetchone()
    assert readings and verdicts
    return {
        **{f"jobs_{r['status']}": r["n"] for r in jobs},
        "readings": readings["n"],
        "verdicts": verdicts["n"],
        "verdict_events": sum(e.type == VERDICT_RECORDED for e in events),
        "ai_read_events": sum(e.type == AI_READ for e in events),
    }


def test_judge_jobs_run_through_the_worker_once(database: Database) -> None:
    world = build_world(database)
    # Small batches, so the stage needs several jobs.
    world.screening = Screening(
        world.ledger, PostgresJobQueue(), PostgresReceipts(), judge_batch_size=8
    )
    with world.db.transaction() as tx:
        info = world.screening.open_stage(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            actor_id=world.people.lead,
            spec=StageSpec(
                protocol_version_id=world.protocol.id,
                scope_id=SCOPE,
                stage=STAGE,
                records=tuple(world.records),
                screeners=(world.people.a, world.people.b),
                auditor_id=world.people.auditor,
                adjudicators=(world.people.adjudicator,),
                sentinels=SENTINELS,
                judge_config_id=CONFIG.id,
            ),
        )
        for _ in range(2):  # asking twice enqueues the same jobs once
            world.screening.request_judging(
                tx,
                command_id=new_id(),
                project_id=world.project_id,
                actor_id=world.people.lead,
                stage_id=info.stage_id,
            )
        # Nothing is queued for reviewers before the AI has read it.
        assert world.screening.queue(tx, world.project_id, world.people.a, SCOPE, STAGE) == []
    items = len(world.records) + 1  # one sentinel
    assert tally(world, info.stage_id) == {
        "jobs_queued": 4,
        "readings": 0,
        "verdicts": 0,
        "verdict_events": 0,
        "ai_read_events": 0,
    }

    backend = recorded_backend([*world.records, *(s.record for s in SENTINELS)], world.criteria())
    assert world.run_judge_jobs(backend) == 4
    assert len(backend.requests) == items  # one model call per item
    done = tally(world, info.stage_id)
    assert done == {
        "jobs_succeeded": 4,
        "readings": items,
        "verdicts": items,
        "verdict_events": items,
        "ai_read_events": 4,
    }

    # The AI's reading of the colitis trial: a verified exclusion on the population.
    with world.db.transaction() as tx:
        row = tx.execute(
            "select r.excludes, r.rank from screening_ai_reading r join screening_item i"
            " on i.stage_id = r.stage_id and i.item_ref = r.item_ref"
            " where r.stage_id = %s and i.title like 'Transdermal nicotine%%'",
            (info.stage_id,),
        ).fetchone()
        assert row == {"excludes": True, "rank": 1001}  # last in an AI order: one criterion not met
        assert (
            len(world.screening.queue(tx, world.project_id, world.people.a, SCOPE, STAGE, limit=50))
            == items
        )

    # Asking again, or running a finished job again (a worker that crashed after recording
    # its verdicts), calls no model and attaches nothing twice.
    with world.db.transaction() as tx:
        world.screening.request_judging(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            actor_id=world.people.lead,
            stage_id=info.stage_id,
        )
    assert world.run_judge_jobs(backend) == 0
    with world.db.transaction() as tx:
        job = PostgresJobQueue().get_by_stage_key(tx, f"{JUDGE_JOB}:{info.stage_id}:{CONFIG.id}:0")
    handler = judge_handler(
        world.db, world.screening, world.runner(backend), actor_id=world.people.model
    )
    output = handler(job, None)  # type: ignore[arg-type]
    assert output.publish is not None
    with world.db.transaction() as tx:
        output.publish(tx)
    assert len(backend.requests) == items
    assert tally(world, info.stage_id) == done
    with world.db.transaction() as tx:
        assert world.ledger.verify(tx, world.project_id).ok
