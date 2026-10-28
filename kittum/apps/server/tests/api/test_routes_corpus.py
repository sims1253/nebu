"""Tests for corpus HTTP routes."""

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.core import new_id
from kittum.corpus import ingest
from kittum.db import Database
from kittum.ledger.testing import InMemoryLedger
from kittum.search import SearchExecution, SearchPlan, SourceRecord
from kittum.settings import Settings
from kittum.storage import FilesystemStore

if TYPE_CHECKING:
    from collections.abc import Generator

DUMMY_HASH = "0" * 64


@pytest.fixture
def api_client(tmp_path) -> Generator[TestClient]:
    db_url = os.environ.get("KITTUM_DATABASE_URL")
    if not db_url:
        pytest.skip("KITTUM_DATABASE_URL not set")

    settings = Settings(
        database_url=db_url,
        artifact_dir=tmp_path / "artifacts",
    )
    context = AppContext(
        settings=settings,
        db=Database(db_url),
        artifacts=FilesystemStore(settings.artifact_dir),
        ledger=InMemoryLedger(),
    )
    app = create_app(context)
    with TestClient(app) as client:
        yield client


def test_corpus_api_endpoints_e2e(api_client: TestClient) -> None:
    db_url = os.environ.get("KITTUM_DATABASE_URL")
    if not db_url:
        pytest.skip("KITTUM_DATABASE_URL not set")

    db = Database(db_url)
    proj_id = new_id()
    actor_id = new_id()

    now = datetime.now(UTC)
    rec1 = SourceRecord(
        occurrence_id=new_id(),
        source="pubmed",
        provider_record_id="pm-1",
        ordinal=1,
        title="Pembrolizumab in NSCLC",
        doi="10.1056/NEJMoa1606774",
        raw_hash=DUMMY_HASH,
    )
    rec2 = SourceRecord(
        occurrence_id=new_id(),
        source="central",
        provider_record_id="cn-1",
        ordinal=2,
        title="Pembrolizumab in NSCLC",
        doi="10.1056/NEJMoa1606774",
        raw_hash=DUMMY_HASH,
    )
    execution = SearchExecution(
        id=new_id(),
        plan=SearchPlan(
            topic_id="T1",
            source="pubmed",
            platform="PubMed",
            strategy="pembro",
        ),
        kind="live_api",
        started_at=now,
        ended_at=now,
        query_translation=None,
        reported_count=2,
        retrieved_count=2,
        raw_artifact_hashes=(),
        records=(rec1, rec2),
    )

    with db.transaction() as tx:
        tx.execute(
            "insert into project (id, name, policy_version) values (%s, %s, %s)",
            (proj_id, "API Test Project", "1.0"),
        )
        tx.execute(
            "insert into actor (id, identity_ref, kind) values (%s, %s, %s)",
            (actor_id, f"actor-{actor_id}@kittum.org", "human"),
        )
        # The actor leads the project: the catalogue is the lead's alone.
        tx.execute(
            "insert into project_member (project_id, actor_id, role, display_name, event_id)"
            " values (%s, %s, 'lead', 'Lead', %s)",
            (proj_id, actor_id, new_id()),
        )
        ledger = api_client.app.state.context.ledger
        corpus_rev = ingest(tx, proj_id, [execution], actor_id=actor_id, ledger=ledger)

    # 1. GET /api/projects/{project_id}/corpus/proposals, the lead's only
    path = f"/api/projects/{proj_id}/corpus/proposals"
    assert api_client.get(path, headers={"X-Kittum-Reviewer": str(new_id())}).status_code == 403
    resp = api_client.get(path, headers={"X-Kittum-Reviewer": str(actor_id)})
    assert resp.status_code == 200
    proposals = resp.json()
    assert len(proposals) == 1
    prop = proposals[0]
    assert prop["relation"] == "same_record"
    prop_id = prop["id"]

    # 2. GET initial PRISMA counts
    resp = api_client.get(
        f"/api/projects/{proj_id}/corpus/revisions/{corpus_rev.identity_revision_id}/counts"
    )
    assert resp.status_code == 200
    counts = resp.json()
    assert counts["total_records_identified"] == 2
    assert counts["duplicates_removed"] == 0
    assert counts["unique_records"] == 2

    # 3. POST /api/projects/{project_id}/corpus/confirm
    confirm_payload = {
        "revision_id": str(corpus_rev.identity_revision_id),
        "proposal_id": prop_id,
        "actor_id": str(actor_id),
    }
    resp = api_client.post(f"/api/projects/{proj_id}/corpus/confirm", json=confirm_payload)
    assert resp.status_code == 200
    new_rev = resp.json()
    new_rev_id = new_rev["id"]
    assert new_rev["parent_id"] == str(corpus_rev.identity_revision_id)

    # 4. GET counts after confirmation
    resp = api_client.get(f"/api/projects/{proj_id}/corpus/revisions/{new_rev_id}/counts")
    assert resp.status_code == 200
    counts_after = resp.json()
    assert counts_after["total_records_identified"] == 2
    assert counts_after["duplicates_removed"] == 1
    assert counts_after["unique_records"] == 1
    assert counts_after["studies_identified"] == 1

    # 5. POST /api/projects/{project_id}/corpus/split
    split_payload = {
        "revision_id": new_rev_id,
        "occurrence_ids": [str(rec1.occurrence_id), str(rec2.occurrence_id)],
        "level": "report",
        "actor_id": str(actor_id),
        "reason": "Undo merge",
    }
    resp = api_client.post(f"/api/projects/{proj_id}/corpus/split", json=split_payload)
    assert resp.status_code == 200
    split_rev = resp.json()
    split_rev_id = split_rev["id"]

    # 6. GET counts after split
    resp = api_client.get(f"/api/projects/{proj_id}/corpus/revisions/{split_rev_id}/counts")
    assert resp.status_code == 200
    counts_split = resp.json()
    assert counts_split["unique_records"] == 2
    assert counts_split["duplicates_removed"] == 0

    # 7. Another project's revision is not found; a merge needs two occurrences
    other_proj = new_id()
    with db.transaction() as tx:
        tx.execute(
            "insert into project (id, name, policy_version) values (%s, %s, %s)",
            (other_proj, "Other API Test Project", "1.0"),
        )
    resp = api_client.get(f"/api/projects/{other_proj}/corpus/revisions/{split_rev_id}/counts")
    assert resp.status_code == 404
    merge_payload = {
        "revision_id": split_rev_id,
        "occurrence_ids": [str(rec1.occurrence_id), str(rec2.occurrence_id)],
        "relation": "same_record",
        "actor_id": str(actor_id),
    }
    resp = api_client.post(f"/api/projects/{other_proj}/corpus/merge", json=merge_payload)
    assert resp.status_code == 404
    for occurrence_ids in ([], [str(rec1.occurrence_id)] * 2):
        resp = api_client.post(
            f"/api/projects/{proj_id}/corpus/merge",
            json={**merge_payload, "occurrence_ids": occurrence_ids},
        )
        assert resp.status_code == 400

    # 8. A study split keeps a confirmed duplicate pair together as one record
    resp = api_client.post(
        f"/api/projects/{proj_id}/corpus/split",
        json={**split_payload, "revision_id": new_rev_id, "level": "study"},
    )
    assert resp.status_code == 200
    resp = api_client.get(f"/api/projects/{proj_id}/corpus/revisions/{resp.json()['id']}/counts")
    assert resp.json()["unique_records"] == 1
