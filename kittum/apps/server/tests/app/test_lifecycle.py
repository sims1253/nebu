"""A project's lifecycle through the HTTP API, from nothing to an open stage the AI has read.

The steps the demo seed takes in one call, taken one route at a time as an integrator would:
the project and its people, the protocol, two file imports, the corpus, and a title and abstract
stage. (Full texts are left to the end-to-end test: the documents tests expect an empty document
table in the shared test database.) The worker runs the stage's judge jobs on the recorded answers; a worker
whose judge is a different one refuses them before any model call.
"""

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app import Judge, job_handlers, judge_config
from kittum.app.demo import DATA, PROTOCOL, SENTINELS, demo_judge
from kittum.extraction import JOB_KIND as EXTRACTION_JOB
from kittum.ledger import PostgresLedger
from kittum.models import ModelRegistry
from kittum.models.testing import RecordedBackend
from kittum.runs import PostgresJobQueue, PostgresReceipts, Worker
from kittum.screening import JUDGE_JOB
from kittum.settings import Settings
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Database

pytestmark = pytest.mark.postgres


def _context(database: Database, tmp_path: Path, judge: Judge) -> AppContext:
    return AppContext(
        settings=Settings(database_url="postgresql://unused", artifact_dir=tmp_path),
        db=database,
        artifacts=MemoryStore(),
        ledger=PostgresLedger(),
        judge=judge,
    )


def _run_jobs(ctx: AppContext) -> int:
    worker = Worker(ctx.db, PostgresJobQueue(), job_handlers(ctx), name="lifecycle-test")
    ran = 0
    while worker.run_once():
        ran += 1
    return ran


def test_a_project_from_nothing_to_an_open_stage(database: Database, tmp_path: Path) -> None:
    registry = ModelRegistry.load()
    ctx = _context(database, tmp_path, demo_judge(registry))
    client = TestClient(create_app(ctx))

    def ok(response: Any, status: int = 200) -> Any:
        assert response.status_code == status, response.text
        return response.json()

    project = ok(
        client.post("/api/projects", json={"name": "NRT update", "lead_name": "Morgan Lee"}), 201
    )
    pid = project["id"]
    (lead,) = [m["actor_id"] for m in project["members"] if m["roles"] == ["lead"]]
    for name, roles in (
        ("Sam Okafor", ["reviewer"]),
        ("Jun Park", ["reviewer"]),
        ("Rita Nunes", ["auditor"]),
        ("Elena Weiss", ["adjudicator"]),
        ("Ola Berg", ["reviewer"]),  # a third reviewer, whom M1 does not need
    ):
        body = {"display_name": name, "roles": roles, "added_by": lead}
        ok(client.post(f"/api/projects/{pid}/members", json=body), 201)
    people = {
        m["display_name"]: m["actor_id"] for m in ok(client.get(f"/api/projects/{pid}"))["members"]
    }
    as_lead = {"X-Kittum-Reviewer": lead}

    # Nothing to screen before a protocol, searches, and a corpus.
    stage = {"scope_id": "nrt-vs-control", "stage": "title_abstract", "actor_id": lead}
    refused = client.post(f"/api/projects/{pid}/screening/stages", json=stage)
    assert refused.status_code == 409 and "publish the protocol" in refused.text

    draft = ok(
        client.post(
            f"/api/projects/{pid}/protocol-versions", json=yaml.safe_load(PROTOCOL.read_text())
        ),
        201,
    )
    ok(
        client.post(
            f"/api/projects/{pid}/protocol-versions/{draft['id']}/publish",
            json={"approved_by": lead},
        )
    )
    published = ok(client.get(f"/api/projects/{pid}/protocol"))
    assert published["labels"]["slot.population"] == "Population"  # no profile: the core's words

    for source, platform, file in (
        ("central", "Cochrane Library (Wiley)", "central_export.ris"),
        ("citation_search", "Reference lists", "citation_search.ris"),
    ):
        body = {
            "plan": {"topic_id": "TRIALS", "source": source, "platform": platform, "strategy": "x"},
            "data": (DATA / file).read_text(),
            "format": "ris",
            "actor_id": lead,
        }
        ok(client.post(f"/api/projects/{pid}/search/import", json=body))
    ok(client.post(f"/api/projects/{pid}/corpus/ingest", json={"actor_id": lead}), 201)
    again = client.post(f"/api/projects/{pid}/corpus/ingest", json={"actor_id": lead})
    assert again.status_code == 409  # update searches need the corpus diff, not a second corpus

    corpus = ok(client.get(f"/api/projects/{pid}/corpus/revisions/latest", headers=as_lead))
    assert len(corpus["reports"]) == 10  # seven CENTRAL records and three found by citation
    sentinels = [s.model_dump(mode="json") for s in SENTINELS]
    info = ok(
        client.post(f"/api/projects/{pid}/screening/stages", json=stage | {"sentinels": sentinels}),
        201,
    )
    assert info["mode"] == "M1" and info["judge_config_id"] == ctx.judge.config.id  # type: ignore[union-attr]
    project = ok(client.get(f"/api/projects/{pid}"))
    assert [s["stage_id"] for s in project["stages"]] == [info["stage_id"]]
    reviewer = {"X-Kittum-Reviewer": project["stages"][0]["screeners"][0]}
    queue = {"scope_id": "nrt-vs-control", "stage": "title_abstract"}
    assert (
        ok(client.get(f"/api/projects/{pid}/screening/queue", params=queue, headers=reviewer)) == []
    )

    # A worker with another judge refuses the stage's jobs without calling its model.
    other = RecordedBackend(default="{}")
    stranger = _context(
        database,
        tmp_path,
        Judge(
            config=judge_config("qwen3.5-4b-ollama", label="another-judge"),
            recipe=registry.get("qwen3.5-4b-ollama"),
            backend=other,
        ),
    )
    assert set(job_handlers(stranger)) == {JUDGE_JOB}  # no model server, so no extraction
    with_models = replace(
        stranger,
        backends={"qwen3.5-4b-ollama": other},
        queue=PostgresJobQueue(),
        receipts=PostgresReceipts(),
    )
    assert set(job_handlers(with_models)) == {JUDGE_JOB, EXTRACTION_JOB}
    with database.transaction() as tx:
        jobs = tx.execute(
            "select id from job where project_id = %s and kind = %s", (pid, JUDGE_JOB)
        ).fetchall()
        tx.execute(
            "update job set run_after = now() + interval '1 hour' where project_id <> %s"
            " and status = 'queued'",
            (pid,),
        )
    assert jobs
    assert _run_jobs(stranger) >= 1
    assert other.requests == []
    with database.transaction() as tx:
        failed = tx.execute(
            "select status, last_error from job where project_id = %s", (pid,)
        ).fetchall()
        # Requeue them for the right worker: a refused job fails for good, so this stands in for the
        # operator who started the wrong worker and now starts the right one.
        tx.execute(
            "update job set status = 'queued', attempt = 0, run_after = now() where project_id = %s",
            (pid,),
        )
    assert {f["status"] for f in failed} == {"failed"}
    assert "needs demo-recorded" in failed[0]["last_error"]

    assert _run_jobs(ctx) == len(jobs)
    items = ok(
        client.get(
            f"/api/projects/{pid}/screening/queue", params=queue | {"limit": 50}, headers=reviewer
        )
    )
    assert len(items) == 11  # ten records and one sentinel, now that the AI has read them

    progress = ok(client.get(f"/api/projects/{pid}/progress", headers=as_lead))
    assert progress["identification"]["total_records_identified"] == 10
    assert progress["full_texts"] == 0
    (ta,) = progress["stages"]
    assert ta["status"]["view"] == "lead" and ta["outcomes"] == {"pending": 10}
    # A screener sees only their own counts, and no outcomes.
    mine = ok(client.get(f"/api/projects/{pid}/progress", headers=reviewer))
    assert mine["stages"][0]["status"]["view"] == "reviewer"
    assert mine["stages"][0]["outcomes"] is None
    assert mine["identification"] is None and mine["searches"] == 0

    # Only the lead reads the whole catalogue: anyone else could tell the sentinels in their
    # queue by their absence from it.
    catalogue = ("corpus/revisions/latest", "corpus/proposals", "search/executions")
    for person in ("Sam Okafor", "Rita Nunes", "Ola Berg"):
        for path in catalogue:
            read = client.get(
                f"/api/projects/{pid}/{path}", headers={"X-Kittum-Reviewer": people[person]}
            )
            assert read.status_code == 403, (person, path)
    for path in catalogue:
        ok(client.get(f"/api/projects/{pid}/{path}", headers=as_lead))

    # A reviewer with no part in the stage gets nothing of it: no lead view by default.
    outsider = {"X-Kittum-Reviewer": people["Ola Berg"]}
    assert people["Ola Berg"] not in info["screeners"]
    theirs = ok(client.get(f"/api/projects/{pid}/progress", headers=outsider))
    assert theirs["stages"] == [] and theirs["identification"] is None
    unseen = client.get(f"/api/projects/{pid}/screening/status", params=queue, headers=outsider)
    assert unseen.status_code == 404

    ledger = ok(client.get(f"/api/projects/{pid}/ledger/verify"))
    assert ledger["verify"]["ok"]
    assert ledger["by_type"]["project.member_added"] == 5
    assert ledger["by_type"]["judge.verdict_recorded"] == 11
