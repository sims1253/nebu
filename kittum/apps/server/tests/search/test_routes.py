import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from kittum.api import AppContext, create_app
from kittum.core import new_id, sha256_hex
from kittum.db import Database
from kittum.db.testing import null_tx
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.ledger.testing import InMemoryLedger
from kittum.settings import Settings
from kittum.storage import MemoryStore

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).parent / "fixtures"

STRATEGY = "lumacaftor[ti] AND trial[tiab]"
ESEARCH_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
    "db=pubmed&term=lumacaftor%5Bti%5D%20AND%20trial%5Btiab%5D&usehistory=y&retmode=xml&tool=kittum"
)
EFETCH_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
    "db=pubmed&query_key=1&WebEnv=MCID_6ae14800045d3e9e3004e3fd&retstart=0&retmax=24&retmode=xml&tool=kittum"
)
PUBMED_PLAN = {
    "topic_id": "topic-api",
    "source": "pubmed",
    "platform": "PubMed",
    "strategy": STRATEGY,
    "limits": [],
}
CENTRAL_PLAN = {
    "topic_id": "topic-api",
    "source": "central",
    "platform": "Cochrane",
    "strategy": "my-strategy",
    "limits": [],
}


def _insert(url: str, sql: str) -> str:
    with psycopg.Connection.connect(url, row_factory=dict_row) as conn:
        row = conn.execute(sql).fetchone()
        conn.commit()
    assert row
    return str(row["id"])


@pytest.fixture
def url() -> str:
    if not os.environ.get("KITTUM_DATABASE_URL"):
        pytest.skip("KITTUM_DATABASE_URL is not set")
    return os.environ["KITTUM_DATABASE_URL"]


@pytest.fixture
def actor_id(url: str) -> str:
    return _insert(
        url,
        f"insert into actor (identity_ref, kind) values ('api-{new_id()}', 'human') returning id",
    )


def _lead(url: str, project_id: str, actor_id: str) -> None:
    """Make the actor the project's methods lead, who alone reads its records."""
    _insert(
        url,
        "insert into project_member (project_id, actor_id, role, display_name, event_id)"
        f" values ('{project_id}', '{actor_id}', 'lead', 'Lead', uuidv7()) returning actor_id as id",
    )


def _project(url: str) -> str:
    return _insert(
        url, "insert into project (name, policy_version) values ('api-test', 'v1') returning id"
    )


@pytest.fixture
def context(url: str) -> Iterator[AppContext]:
    transport = FakeTransport(
        {
            ESEARCH_URL: (FIXTURES / "pubmed_esearch.xml").read_bytes(),
            EFETCH_URL: (FIXTURES / "pubmed_efetch.xml").read_bytes(),
        }
    )
    db = Database(url)
    yield AppContext(
        settings=Settings(database_url=url, artifact_dir=Path("/tmp/artifacts")),
        db=db,
        artifacts=MemoryStore(),
        gateway=PassThroughGateway(transport),
        ledger=InMemoryLedger(),
    )
    db.close()


def test_search_routes_end_to_end(url: str, actor_id: str, context: AppContext) -> None:
    project_id = _project(url)
    assert context.ledger is not None

    with TestClient(create_app(context)) as client:
        # 1. Run a live PubMed search via the API
        run_resp = client.post(
            f"/api/projects/{project_id}/search/run",
            json={"plan": PUBMED_PLAN, "actor_id": actor_id},
        )
        assert run_resp.status_code == 200, run_resp.text
        run_data = run_resp.json()
        exec_id = run_data["id"]
        assert run_data["reported_count"] == 24
        assert run_data["retrieved_count"] == 24
        assert len(run_data["records"]) == 24
        for raw_hash in run_data["raw_artifact_hashes"]:
            assert context.artifacts.exists(raw_hash)

        # 2. Import file via API; the file is stored as the execution's raw artifact
        ris_bytes = (FIXTURES / "central_export.ris").read_bytes()
        import_resp = client.post(
            f"/api/projects/{project_id}/search/import",
            json={
                "plan": CENTRAL_PLAN,
                "data": ris_bytes.decode(),
                "format": "central",
                "actor_id": actor_id,
            },
        )
        assert import_resp.status_code == 200, import_resp.text
        import_data = import_resp.json()
        assert import_data["reported_count"] == 2
        assert import_data["raw_artifact_hashes"] == [sha256_hex(ris_bytes)]
        assert context.artifacts.exists(sha256_hex(ris_bytes))

        # 3. Both executions are in the ledger, by the requesting actor
        events = context.ledger.events(null_tx(), uuid.UUID(project_id))
        assert [e.payload["execution_id"] for e in events] == [exec_id, import_data["id"]]
        assert {str(e.actor_id) for e in events} == {actor_id}

        # 4. List executions: the records are the lead's alone, so a reviewer cannot tell the
        # sentinels in their queue by their absence from the catalogue.
        executions = f"/api/projects/{project_id}/search/executions"
        reviewer = {"X-Kittum-Reviewer": actor_id}
        assert client.get(executions, headers=reviewer).status_code == 403
        _lead(url, project_id, actor_id)
        list_resp = client.get(executions, headers=reviewer)
        assert list_resp.status_code == 200
        assert {e["id"] for e in list_resp.json()} == {exec_id, import_data["id"]}

        # 5. Get specific execution
        get_resp = client.get(f"{executions}/{exec_id}", headers=reviewer)
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["id"] == exec_id
        assert [r["occurrence_id"] for r in get_data["records"]] == [
            r["occurrence_id"] for r in run_data["records"]
        ]

        # 6. Non-existent execution returns 404
        not_found_resp = client.get(
            f"{executions}/00000000-0000-0000-0000-000000000000", headers=reviewer
        )
        assert not_found_resp.status_code == 404

        # 7. An unknown import format is a 400 that names the supported ones
        bad_resp = client.post(
            f"/api/projects/{project_id}/search/import",
            json={"plan": CENTRAL_PLAN, "data": "x", "format": "endnote", "actor_id": actor_id},
        )
        assert bad_resp.status_code == 400
        assert "supported: central" in bad_resp.json()["detail"]


def test_one_project_cannot_read_another_projects_execution(
    url: str, actor_id: str, context: AppContext
) -> None:
    owner, other = _project(url), _project(url)

    with TestClient(create_app(context)) as client:
        resp = client.post(
            f"/api/projects/{owner}/search/import",
            json={
                "plan": CENTRAL_PLAN,
                "data": (FIXTURES / "central_export.ris").read_text(),
                "format": "central",
                "actor_id": actor_id,
            },
        )
        assert resp.status_code == 200, resp.text
        exec_id = resp.json()["id"]

        for project in (owner, other):
            _lead(url, project, actor_id)
        reviewer = {"X-Kittum-Reviewer": actor_id}
        path = f"search/executions/{exec_id}"
        assert client.get(f"/api/projects/{owner}/{path}", headers=reviewer).status_code == 200
        assert client.get(f"/api/projects/{other}/{path}", headers=reviewer).status_code == 404
        listed = client.get(f"/api/projects/{other}/search/executions", headers=reviewer)
        assert listed.json() == []


def test_search_routes_refuse_to_write_without_a_ledger_or_gateway(
    url: str, actor_id: str, context: AppContext
) -> None:
    project_id = _project(url)
    import_body = {
        "plan": CENTRAL_PLAN,
        "data": (FIXTURES / "central_export.ris").read_text(),
        "format": "central",
        "actor_id": actor_id,
    }

    context.ledger = None
    with TestClient(create_app(context)) as client:
        for path, body in (
            ("run", {"plan": PUBMED_PLAN, "actor_id": actor_id}),
            ("import", import_body),
        ):
            resp = client.post(f"/api/projects/{project_id}/search/{path}", json=body)
            assert resp.status_code == 503
            assert "ledger" in resp.json()["detail"]

    context.ledger = InMemoryLedger()
    context.gateway = None
    with TestClient(create_app(context)) as client:
        resp = client.post(
            f"/api/projects/{project_id}/search/run",
            json={"plan": PUBMED_PLAN, "actor_id": actor_id},
        )
        assert resp.status_code == 503
        assert "gateway" in resp.json()["detail"]
        _lead(url, project_id, actor_id)
        listed = client.get(
            f"/api/projects/{project_id}/search/executions",
            headers={"X-Kittum-Reviewer": actor_id},
        )
        assert listed.json() == []
