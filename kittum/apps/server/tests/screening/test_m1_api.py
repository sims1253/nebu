"""M1 through the HTTP API: two blinded reviewers, the reveal, conflicts, and adjudication."""

import json
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.core import new_id
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.screening.support import JUDGE_MARK, SCOPE, STAGE, build_world, timing

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from kittum.core import Id
    from kittum.db import Database
    from tests.screening.support import World

pytestmark = pytest.mark.postgres

TITLES = {
    "patch": "Nicotine patch versus placebo",
    "gum": "Nicotine gum for smoking",
    "combination": "Combination nicotine replacement",
    "colitis": "Transdermal nicotine for active ulcerative colitis",
    "varenicline": "Varenicline versus placebo",
    "cohort": "Use of nicotine replacement therapy",
    "cf_rct": "Efficacy and safety of lumacaftor and ivacaftor in patients aged 6-11",
    "cf_review": "Lumacaftor/Ivacaftor: A Review",
    "glpg": "GLPG2737 in lumacaftor",
}


class Reviewer:
    """One person at the workbench: every response they receive is kept for the blinding checks."""

    def __init__(self, client: TestClient, world: World, actor_id: Id) -> None:
        self.client = client
        self.world = world
        self.id = actor_id
        self.seen: list[str] = []

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"/api/projects/{self.world.project_id}/screening{path}"
        headers = {"X-Kittum-Reviewer": str(self.id)}
        response = self.client.request(method, url, headers=headers, **kwargs)
        self.seen.append(response.text)
        return response

    def queue(self, after: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"scope_id": SCOPE, "stage": STAGE, "limit": limit}
        if after:
            params["after"] = after
        response = self.call("GET", "/queue", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def whole_queue(self) -> list[dict[str, Any]]:
        """Batches of five, as the workbench prefetches them."""
        items: list[dict[str, Any]] = []
        while batch := self.queue(after=items[-1]["assignment_id"] if items else None):
            items += batch
        return items

    def item(self, key: str) -> dict[str, Any]:
        (found,) = [i for i in self.whole_queue() if i["title"].startswith(TITLES[key])]
        return found

    def decide(
        self,
        key: str,
        outcome: str,
        reason: str | None = None,
        *,
        phase: str = "provisional",
        comment: str | None = None,
    ) -> dict[str, Any]:
        item = self.item(key)
        assert item["phase"] == phase
        response = self.call(
            "POST",
            f"/assignments/{item['assignment_id']}/decide",
            json={
                "command_id": str(new_id()),
                "decision": {
                    "outcome": outcome,
                    "reason_criterion_id": reason,
                    "comment": comment,
                },
                "phase": phase,
                "expected_revision": item["revision"],
                "timing": json.loads(
                    timing(len(self.seen), revealed=phase == "final").model_dump_json()
                ),
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def screen(
        self, key: str, outcome: str, reason: str | None = None, **kw: Any
    ) -> dict[str, Any]:
        """Decide, and keep the decision if the AI is revealed. Returns the first result."""
        first = self.decide(key, outcome, reason, **kw)
        if first["phase"] == "provisional":
            self.decide(key, outcome, reason, phase="final", **kw)
        return first

    def status(self) -> dict[str, Any]:
        response = self.call("GET", "/status", params={"scope_id": SCOPE, "stage": STAGE})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def world(database: Database) -> World:
    world = build_world(database)
    world.records = [world.record(t) for t in TITLES.values()]
    return world


@pytest.fixture
def client(world: World, tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(database_url="postgresql://unused", artifact_dir=tmp_path)
    context = AppContext(
        settings=settings, db=world.db, artifacts=MemoryStore(), ledger=world.ledger
    )
    with TestClient(create_app(context)) as client:
        yield client


def test_m1_blinds_reviewers_reveals_disagreement_and_adjudicates(
    world: World, client: TestClient
) -> None:
    world.open()
    a, b = Reviewer(client, world, world.people.a), Reviewer(client, world, world.people.b)
    adjudicator = Reviewer(client, world, world.people.adjudicator)
    lead = Reviewer(client, world, world.people.lead)

    # Every item of B's queue has the same shape; the sentinel among them is not marked.
    first = b.whole_queue()
    assert len(first) == 10  # nine records and one sentinel (1% of nine, rounded up)
    assert {frozenset(i) for i in first} == {
        frozenset(
            {
                "assignment_id",
                "stage",
                "scope_id",
                "title",
                "text",
                "highlights",
                "revision",
                "record",
                "phase",
                "reveal",
            }
        )
    }
    assert any(i["title"].startswith("Nicotine lozenge") for i in first)
    # A different seeded order for each reviewer.
    assert [i["title"] for i in first] != [i["title"] for i in a.whole_queue()]

    # A screens everything first, with comments only A should ever see.
    note = f"A-note-{new_id()}"
    plan = {
        "patch": ("include", None),
        "gum": ("include", None),
        "combination": ("include", None),
        "colitis": ("exclude", "POP"),
        "varenicline": ("exclude", "INT"),
        "cohort": ("exclude", "DESIGN"),
        "cf_rct": ("include", None),  # A's slip: the population is wrong
        "cf_review": ("exclude", "DESIGN"),
        "glpg": ("exclude", "POP"),
    }
    for key, (outcome, reason) in plan.items():
        result = a.screen(key, outcome, reason, comment=note)
        # Where the AI agrees, A's provisional decision is final at once. The model answered
        # not_met for A's other exclusions (the filter confirmed only the colitis one), so it
        # agrees with them; only for GLPG2737 did it see no failing criterion.
        assert (result["reveal"] is not None) == (key == "glpg"), key
    (sentinel,) = a.whole_queue()
    a.call(
        "POST",
        f"/assignments/{sentinel['assignment_id']}/decide",
        json={
            "command_id": str(new_id()),
            "decision": {"outcome": "include"},
            "phase": "provisional",
            "expected_revision": 0,
            "timing": json.loads(timing(0).model_dump_json()),
        },
    )
    assert a.whole_queue() == []

    # B still sees only B's own work: no A data, no AI output, no shared counts.
    assert len(b.whole_queue()) == 10
    assert b.status() == {
        "scope_id": SCOPE,
        "stage": STAGE,
        "view": "reviewer",
        "screened": 0,
        "remaining": 10,
        "conflicts": None,
        "metrics": None,
    }
    # B cannot reach A's assignments.
    a_done = client.get(
        f"/api/projects/{world.project_id}/screening/queue",
        params={"scope_id": SCOPE, "stage": STAGE},
        headers={"X-Kittum-Reviewer": str(world.people.a)},
    ).json()
    assert a_done == []
    with world.db.transaction() as tx:
        a_assignment = tx.execute(
            "select id from screening_assignment where reviewer_id = %s limit 1", (world.people.a,)
        ).fetchone()
    assert a_assignment is not None
    stolen = b.call(
        "POST",
        f"/assignments/{a_assignment['id']}/undo",
        json={"command_id": str(new_id()), "expected_revision": 1},
    )
    assert stolen.status_code == 404

    # B agrees on the clear includes: no reveal.
    for key in ("patch", "gum", "combination"):
        assert b.decide(key, "include")["phase"] == "final"

    # B includes the colitis trial; the AI excludes on a verified quote, so it is revealed.
    revealed = b.decide("colitis", "include")
    assert revealed["phase"] == "provisional"
    assert revealed["reveal"] == {
        "criterion_id": "POP",
        "status": "not_met",
        "quote": "did not smoke tobacco",
        "rationale": f"The patients were non-smokers. {JUDGE_MARK}",
    }
    # Until B decides finally, the item stays in B's queue with B's own reveal.
    waiting = b.item("colitis")
    assert waiting["phase"] == "final" and waiting["reveal"] == revealed["reveal"]
    assert b.decide("colitis", "exclude", "POP", phase="final")["phase"] == "final"

    # B excludes the CF trial on the population. The model said not_met too and only the filter
    # downgraded it, so there is no disagreement to reveal.
    settled = b.decide("cf_rct", "exclude", "POP")
    assert (settled["phase"], settled["reveal"]) == ("final", None)

    # The same for the records whose not_met the filter downgraded: no reveal, no escalation.
    for key, reason in (("varenicline", "INT"), ("cohort", "DESIGN"), ("cf_review", "DESIGN")):
        assert b.decide(key, "exclude", reason)["reveal"] is None
    # GLPG2737: the model saw every criterion met, so B's exclusion is revealed.
    shown = b.screen("glpg", "exclude", "POP")
    assert shown["reveal"]["criterion_id"] == "POP" and shown["reveal"]["status"] == "met"
    (b_sentinel,) = b.whole_queue()
    b.call(
        "POST",
        f"/assignments/{b_sentinel['assignment_id']}/decide",
        json={
            "command_id": str(new_id()),
            "decision": {"outcome": "include"},
            "phase": "provisional",
            "expected_revision": 0,
            "timing": json.loads(timing(0).model_dump_json()),
        },
    )

    # Blinding held for everything B ever received: no trace of A, of A's comments, or of the
    # AI except the reveals that followed B's own provisional decisions.
    for text in b.seen:
        assert str(world.people.a) not in text and note not in text
        assert "quote_verified" not in text and "excludes" not in text
    ai_texts = [json.loads(t) for t in b.seen if JUDGE_MARK in t]
    assert ai_texts
    for body in ai_texts:
        if isinstance(body, dict):  # a decide response: the reveal after B's provisional decision
            assert body["phase"] == "provisional" and body["reveal"] is not None
        else:  # a queue item waiting for B's final decision, with B's own reveal
            for item in body:
                assert (JUDGE_MARK in json.dumps(item)) == (item["phase"] == "final")

    # The lead sees two conflicts: the human disagreement on the CF trial, and GLPG2737, which
    # both humans exclude while the model saw no failing criterion. The records whose not_met
    # the filter downgraded are not escalated: the model agrees with the humans.
    status = lead.status()
    assert (status["view"], status["screened"], status["remaining"], status["conflicts"]) == (
        "lead",
        9,
        0,
        2,
    )

    # The adjudicator sees A and B side by side, pseudonymized, without the AI. A decoy is mixed
    # in: a record both humans excluded and the model agreed with, so the queue does not reveal
    # that the AI argued for GLPG2737.
    queue = adjudicator.call("GET", "/adjudications", params={"scope_id": SCOPE, "stage": STAGE})
    conflicts = queue.json()
    agreed = {TITLES[k] for k in ("colitis", "varenicline", "cohort", "cf_review")}
    genuine = [c for c in conflicts if c["title"].startswith((TITLES["cf_rct"], TITLES["glpg"]))]
    (decoy,) = [c for c in conflicts if c not in genuine]
    assert len(genuine) == 2 and decoy["title"].startswith(tuple(agreed))
    assert JUDGE_MARK not in queue.text
    assert str(world.people.a) not in queue.text and str(world.people.b) not in queue.text
    by_title = {c["title"]: c for c in conflicts}
    cf = next(c for t, c in by_title.items() if t.startswith(TITLES["cf_rct"]))
    assert sorted(col["outcome"] for col in cf["columns"]) == ["exclude", "include"]
    assert {col["label"] for col in cf["columns"]} == {"A", "B"}

    # Only the stage's adjudicators adjudicate; a screener has no adjudication queue.
    listed = a.call("GET", "/adjudications", params={"scope_id": SCOPE, "stage": STAGE})
    assert listed.status_code == 404
    refused = a.call(
        "POST",
        f"/adjudications/{cf['conflict_id']}/decide",
        json={"command_id": str(new_id()), "expected_revision": cf["revision"], "adopted": "A"},
    )
    assert refused.status_code == 404

    # The adjudicator adopts the exclusion; the AI column opens after the commit.
    excluder = next(col["label"] for col in cf["columns"] if col["outcome"] == "exclude")
    resolved = adjudicator.call(
        "POST",
        f"/adjudications/{cf['conflict_id']}/decide",
        json={
            "command_id": str(new_id()),
            "expected_revision": cf["revision"],
            "adopted": excluder,
        },
    ).json()
    assert resolved["outcome"] == "exclude" and resolved["reason_criterion_id"] == "POP"
    assert resolved["ai"]["excludes"] is False
    pop = next(c for c in resolved["ai"]["criteria"] if c["criterion_id"] == "POP")
    assert (pop["status"], pop["downgraded_from"]) == ("unclear", "not_met")

    # The AI-include case: the adjudicator decides on their own and confirms the exclusion.
    glpg = next(c for t, c in by_title.items() if t.startswith(TITLES["glpg"]))
    assert [col["outcome"] for col in glpg["columns"]] == ["exclude", "exclude"]
    # The decoy looks the same: the same fields, revision, column labels, and outcomes.
    for item in (glpg, decoy):
        assert set(item) == set(cf) and item["revision"] == 0
        assert [(col["label"], col["outcome"], set(col)) for col in item["columns"]] == [
            ("A", "exclude", set(cf["columns"][0])),
            ("B", "exclude", set(cf["columns"][0])),
        ]
    own = adjudicator.call(
        "POST",
        f"/adjudications/{glpg['conflict_id']}/decide",
        json={
            "command_id": str(new_id()),
            "expected_revision": glpg["revision"],
            "decision": {"outcome": "exclude", "reason_criterion_id": "POP"},
            "timing": json.loads(timing(0).model_dump_json()),
        },
    ).json()
    assert own["outcome"] == "exclude"
    assert {c["status"] for c in own["ai"]["criteria"]} == {"met"}
    remaining = adjudicator.call(
        "GET", "/adjudications", params={"scope_id": SCOPE, "stage": STAGE}
    ).json()
    assert [c["conflict_id"] for c in remaining] == [decoy["conflict_id"]]

    # The adjudicator includes the decoy. Its adjudication is recorded but changes nothing.
    adjudicator.call(
        "POST",
        f"/adjudications/{decoy['conflict_id']}/decide",
        json={
            "command_id": str(new_id()),
            "expected_revision": 0,
            "decision": {"outcome": "include"},
        },
    )
    with world.db.transaction() as tx:
        outcomes = world.screening.outcomes(tx, world.project_id, SCOPE, STAGE)
    by_record = {r.title: outcomes[r.item_ref] for r in world.records}
    assert by_record[decoy["title"]] == "exclude"
    assert by_record[cf["title"]] == "exclude" and by_record[glpg["title"]] == "exclude"

    # The methods lead's metrics.
    metrics = lead.status()["metrics"]
    assert metrics["conflicts_resolved"] == 2
    assert metrics["decoys"] == {
        "cases": 1,
        "adjudicated": 1,
        "matching": 0,
        "held": 0,
        "released_without_cover": 0,
    }
    # One reveal for A (GLPG2737), two for B (colitis, GLPG2737); B revised colitis.
    assert metrics["reveals_shown"] == 3 and metrics["revised_after_reveal"] == 1
    humans = next(p for p in metrics["agreement"] if p["kind"] == "human_human")
    # Blind provisional decisions: they disagree on two of nine (colitis, CF trial).
    assert humans["items"] == 9 and humans["pabak"] == pytest.approx(5 / 9)
    assert humans["kappa"] == pytest.approx(0.55)
    to_ai = {p["raters"][0]: p for p in metrics["agreement"] if p["kind"] == "human_ai"}
    assert to_ai[str(world.people.a)]["items"] == 9
    assert metrics["audit"]["sentinels"] == 2 and metrics["audit"]["sentinels_correct"] == 2
    assert metrics["stop_test"] is None

    with world.db.transaction() as tx:
        assert world.ledger.verify(tx, world.project_id).ok
