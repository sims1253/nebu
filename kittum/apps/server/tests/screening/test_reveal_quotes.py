"""Why the AI disagreed: the reveal and the adjudicator's AI column name the criterion, the
verdict, the rationale, and the quote, placed in the record only when the judge verified it."""

import re
from typing import TYPE_CHECKING

import pytest

from kittum.core import new_id
from kittum.screening import Decision, QuoteSpan
from tests.screening.support import SCOPE, STAGE, answer, build_world, recorded_backend, timing

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.db import Database
    from kittum.screening import QueueItem, Reveal
    from tests.screening.support import World

pytestmark = pytest.mark.postgres

COHORT = "Use of nicotine replacement therapy"  # the model: DESIGN not_met, quote verified
GLPG = "GLPG2737 in lumacaftor"
# The model calls the population met on a passage that is not in the record.
INVENTED = "adult smokers aged 18 to 65 years"


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def at(item: QueueItem, span: QuoteSpan) -> str:
    return (item.title if span.part == "title" else item.text)[span.start : span.end]


def provisional(world: World, reviewer: Id, item: QueueItem, decision: Decision) -> Reveal | None:
    with world.db.transaction() as tx:
        return world.screening.decide(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            reviewer_id=reviewer,
            assignment_id=item.assignment_id,
            decision=decision,
            phase="provisional",
            expected_etag=item.etag,
            timing=timing(0),
        ).reveal


def queue(world: World, reviewer: Id) -> dict[str, QueueItem]:
    with world.db.transaction() as tx:
        items = world.screening.queue(tx, world.project_id, reviewer, SCOPE, STAGE, limit=50)
    return {i.title: i for i in items}


def find(items: dict[str, QueueItem], prefix: str) -> QueueItem:
    (found,) = [i for t, i in items.items() if t.startswith(prefix)]
    return found


def test_the_reveal_places_a_verified_quote_and_never_an_unverified_one(
    database: Database,
) -> None:
    world = build_world(database)
    world.records = [world.record(COHORT), world.record(GLPG)]
    glpg = world.record(GLPG)
    backend = recorded_backend(
        [*world.records],
        world.criteria(),
        answers={
            glpg.item_ref: answer(
                POP=("met", INVENTED, "The trial enrolled adult smokers."),
                INT=("met", "lumacaftor/ivacaftor-treated", "One arm receives a regimen."),
                DESIGN=("met", "A randomized phase 2A trial", "A randomised trial."),
            )
        },
    )
    world.open(backend=backend)
    a, b = world.people.a, world.people.b

    # A includes the cohort study. The AI's verified reason to exclude is shown: the design
    # criterion, its verdict, its rationale, and where its quote is in the abstract.
    cohort = find(queue(world, a), COHORT)
    reveal = provisional(world, a, cohort, Decision(outcome="include"))
    assert reveal is not None
    assert (reveal.criterion_id, reveal.status) == ("DESIGN", "not_met")
    assert reveal.rationale.startswith("A cohort study, not a trial.")
    assert reveal.quote_verified
    assert reveal.quote_span is not None
    assert flat(at(cohort, reveal.quote_span)).lower() == flat(reveal.quote).lower()

    # The reveal comes back with the item while it waits for A's final decision.
    again = find(queue(world, a), COHORT)
    assert again.phase == "final" and again.reveal == reveal

    # A excludes GLPG2737 on the population. The model read the population as met, on a passage
    # that is not in the record. The judge counts that as unclear, and the reveal says so: the
    # model's words, with no place in the text.
    item = find(queue(world, a), GLPG)
    reveal = provisional(world, a, item, Decision(outcome="exclude", reason_criterion_id="POP"))
    assert reveal is not None
    assert (reveal.criterion_id, reveal.status, reveal.quote) == ("POP", "unclear", INVENTED)
    assert reveal.downgraded_from == "met"
    assert not reveal.quote_verified
    assert reveal.quote_span is None
    assert INVENTED not in item.text and INVENTED not in item.title

    # B excludes the cohort study, so it goes to adjudication. Once the adjudicator commits, the
    # AI column places each verified quote and leaves the rest unplaced.
    for title, item in queue(world, a).items():
        if title.startswith(COHORT):
            with world.db.transaction() as tx:
                world.screening.decide(
                    tx,
                    command_id=new_id(),
                    project_id=world.project_id,
                    reviewer_id=a,
                    assignment_id=item.assignment_id,
                    decision=Decision(outcome="include"),
                    phase="final",
                    expected_etag=item.etag,
                    timing=timing(0, revealed=True),
                )
    cohort_b = find(queue(world, b), COHORT)
    assert cohort_b.reveal is None  # nothing of the AI before B's own decision
    excluded = Decision(outcome="exclude", reason_criterion_id="DESIGN")
    assert provisional(world, b, cohort_b, excluded) is None  # the AI agrees with B
    with world.db.transaction() as tx:
        (conflict,) = world.screening.adjudication_queue(
            tx, world.project_id, world.people.adjudicator, SCOPE, STAGE
        )
        result = world.screening.adjudicate(
            tx,
            command_id=new_id(),
            project_id=world.project_id,
            adjudicator_id=world.people.adjudicator,
            conflict_id=conflict.conflict_id,
            expected_etag=conflict.etag,
            decision=excluded,
        )
    design = next(c for c in result.ai.criteria if c.criterion_id == "DESIGN")
    span = result.ai.quote_spans["DESIGN"]
    source = conflict.title if span.part == "title" else conflict.text
    assert design.quote is not None
    assert flat(source[span.start : span.end]).lower() == flat(design.quote).lower()
    unverified = {c.criterion_id for c in result.ai.criteria if not c.quote_verified}
    assert not unverified & set(result.ai.quote_spans)
