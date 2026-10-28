"""Two search topics that screen the same scope: each stage's queue and counts stay its own."""

import copy
from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.ledger import PostgresLedger
from kittum.protocol import compile, publish, save_draft
from kittum.screening import InvalidDecision, Screening, StageSpec
from kittum.storage import MemoryStore
from tests.screening.support import (
    CONFIG,
    SCOPE,
    SENTINELS,
    STAGE,
    World,
    central_import,
    protocol_source,
    recorded_backend,
    screening_records,
    seed_people,
)

if TYPE_CHECKING:
    from kittum.db import Database

pytestmark = pytest.mark.postgres


def two_topics() -> dict:
    """The example with a second topic on the same question, with its own criteria."""
    source = protocol_source()
    first = source["search_topics"][0]
    second = copy.deepcopy(first)
    second["id"] = "TRIALS-UPDATE"
    second["label"] = {"en": "Randomized trials of NRT, update search"}
    second["scopes"] = [SCOPE]
    second["criteria"] = [
        {**c, "id": f"{c['id']}-U", "applies_to": ["TRIALS-UPDATE"]}
        for c in first["criteria"]
        if "title_abstract" in c["decidable_at"]
    ]
    source["search_topics"].append(second)
    return source


def test_topic_id_scopes_queue_and_progress(database: Database) -> None:
    ledger = PostgresLedger()
    artifacts = MemoryStore()
    with database.transaction() as tx:
        people = seed_people(tx)
        draft = save_draft(tx, people.project_id, compile(two_topics()))
        stored = publish(tx, people.project_id, draft.id, people.lead, ledger=ledger)
        records = screening_records(
            tx, people, [central_import(tx, people, ledger, artifacts)], ledger
        )
    world = World(database, ledger, Screening.postgres(ledger), people, stored, records)
    a = people.a
    split = {"TRIALS": records[:4], "TRIALS-UPDATE": records[4:]}

    with database.transaction() as tx:
        for topic, recs in split.items():
            info = world.screening.open_stage(
                tx,
                command_id=new_id(),
                project_id=people.project_id,
                actor_id=people.lead,
                spec=StageSpec(
                    protocol_version_id=stored.id,
                    scope_id=SCOPE,
                    stage=STAGE,
                    topic_id=topic,
                    records=tuple(recs),
                    screeners=(a, people.b),
                    adjudicators=(people.adjudicator,),
                    sentinels=SENTINELS,
                    judge_config_id=CONFIG.id,
                ),
            )
            assert info.topic_id == topic
            world.screening.request_judging(
                tx,
                command_id=new_id(),
                project_id=people.project_id,
                actor_id=people.lead,
                stage_id=info.stage_id,
            )
    # The update topic's criteria have other IDs, so the recorded answers do not fit them; the
    # judge records `invalid_output`, which reads as unclear. Every item is read either way.
    world.run_judge_jobs(recorded_backend(records, world.criteria_of("TRIALS")))

    with database.transaction() as tx:
        for topic, recs in split.items():
            items = world.screening.queue(
                tx, people.project_id, a, SCOPE, STAGE, topic_id=topic, limit=50
            )
            # The topic's records and its one sentinel, nothing of the other topic.
            assert len(items) == len(recs) + 1
            assert {i.title for i in items} - {s.record.title for s in SENTINELS} == {
                r.title for r in recs
            }
            own = world.screening.status(tx, people.project_id, a, SCOPE, STAGE, topic_id=topic)
            assert (own.view, own.screened, own.remaining) == ("reviewer", 0, len(recs) + 1)
        with pytest.raises(InvalidDecision, match="several topics"):
            world.screening.queue(tx, people.project_id, a, SCOPE, STAGE)
