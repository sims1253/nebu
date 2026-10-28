import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from kittum.api import AppContext, create_app
from kittum.db import Database
from kittum.gateway.testing import FakeTransport, PassThroughGateway
from kittum.ledger.testing import InMemoryLedger
from kittum.settings import Settings
from kittum.storage import MemoryStore

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).parent / "fixtures"


def test_search_routes_end_to_end() -> None:
    url = os.environ["KITTUM_DATABASE_URL"]

    # Seed project
    with psycopg.Connection.connect(url, row_factory=dict_row) as conn:
        row = conn.execute(
            "insert into project (name, policy_version) values ('api-test', 'v1') returning id"
        ).fetchone()
        conn.commit()
        assert row
        project_id = str(row["id"])

    # Prepare mock transport
    page1_bytes = (FIXTURES / "ctgov_page1.json").read_bytes()
    page2_bytes = (FIXTURES / "ctgov_page2.json").read_bytes()
    token = "ZVNj7o2Elu8o3lpzTd6s47mtmpOQJJxtZPGg2PcW"
    url_p1 = "https://clinicaltrials.gov/api/v2/studies?query.term=lumacaftor&pageSize=100"
    url_p2 = f"https://clinicaltrials.gov/api/v2/studies?query.term=lumacaftor&pageSize=100&pageToken={token}"

    transport = FakeTransport({url_p1: page1_bytes, url_p2: page2_bytes})
    gateway = PassThroughGateway(transport)
    artifacts = MemoryStore()
    ledger = InMemoryLedger()
    settings = Settings(database_url=url, artifact_dir=Path("/tmp/artifacts"))

    context = AppContext(
        settings=settings,
        db=Database(url),
        artifacts=artifacts,
        gateway=gateway,
        ledger=ledger,
    )

    with TestClient(create_app(context)) as client:
        # 1. Run live search via API
        run_resp = client.post(
            f"/api/projects/{project_id}/search/run",
            json={
                "plan": {
                    "topic_id": "topic-api",
                    "source": "ctgov",
                    "platform": "ClinicalTrials.gov",
                    "strategy": "lumacaftor",
                    "limits": [],
                }
            },
        )
        assert run_resp.status_code == 200, run_resp.text
        run_data = run_resp.json()
        exec_id = run_data["id"]
        assert run_data["reported_count"] == 10
        assert len(run_data["records"]) == 10

        # 2. Import file via API
        ris_text = (FIXTURES / "central_export.ris").read_text()
        import_resp = client.post(
            f"/api/projects/{project_id}/search/import",
            json={
                "plan": {
                    "topic_id": "topic-api",
                    "source": "central",
                    "platform": "Cochrane",
                    "strategy": "my-strategy",
                    "limits": [],
                },
                "data": ris_text,
                "format": "central",
            },
        )
        assert import_resp.status_code == 200, import_resp.text
        import_data = import_resp.json()
        assert import_data["reported_count"] == 2

        # 3. List executions
        list_resp = client.get(f"/api/projects/{project_id}/search/executions")
        assert list_resp.status_code == 200
        exec_list = list_resp.json()
        assert len(exec_list) >= 2

        # 4. Get specific execution
        get_resp = client.get(f"/api/projects/{project_id}/search/executions/{exec_id}")
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["id"] == exec_id
        assert len(get_data["records"]) == 10

        # 5. Non-existent execution returns 404
        not_found_resp = client.get(
            f"/api/projects/{project_id}/search/executions/00000000-0000-0000-0000-000000000000"
        )
        assert not_found_resp.status_code == 404
