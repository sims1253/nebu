"""`kittum worker` on a deployment with no model server: extraction runs still run.

A run without an agent takes the Janus path only and needs no model; one that asks for an agent
gets its LLM proposals recorded as errors that name the missing backend, instead of a job that
no worker ever claims.
"""

from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from kittum.api import AppContext, create_app
from kittum.app import job_handlers
from kittum.core import new_id
from kittum.documents import PyMuPDFParser, derive, ingest
from kittum.extraction import JOB_KIND, RunRequest, request_run
from kittum.ledger import PostgresLedger
from kittum.people import record_roles
from kittum.runs import Commands, PostgresJobQueue, PostgresReceipts, Worker
from kittum.settings import Settings
from kittum.storage import MemoryStore
from tests.documents.conftest import LESOTHO, new_project, savepoint_db
from tests.extraction.support import AGENT, schema

if TYPE_CHECKING:
    from kittum.db import Tx

pytestmark = pytest.mark.postgres


def test_extraction_runs_without_a_model_server(pg: Tx) -> None:
    artifacts = MemoryStore()
    db = savepoint_db(pg)
    queue = PostgresJobQueue()
    ctx = AppContext(
        settings=Settings(database_url="unused", artifact_dir=Path("unused")),
        db=db,
        artifacts=artifacts,
        ledger=PostgresLedger(),
        queue=queue,
        receipts=PostgresReceipts(),
    )
    assert ctx.backends == {}  # no KITTUM_LLM_ENDPOINTS
    handlers = job_handlers(ctx)
    assert JOB_KIND in handlers

    project = new_project(pg)
    requester = pg.execute(
        "insert into actor (identity_ref, kind) values (%s, 'human') returning id",
        (f"lead-{new_id()}",),
    ).fetchone()
    assert requester is not None
    layer = derive(
        pg, ingest(pg, artifacts, LESOTHO.read_bytes()), PyMuPDFParser(), artifacts=artifacts
    )
    worker = Worker(
        db, queue, {JOB_KIND: handlers[JOB_KIND]}, name="w", lease=timedelta(minutes=30)
    )
    base = f"/api/projects/{project}/extraction/runs"

    def request(agent: dict | None) -> str:
        body = {
            "command_id": str(new_id()),
            "actor_id": str(requester["id"]),
            "layer_id": str(layer.id),
            "study_id": str(new_id()),
            "report_id": str(new_id()),
            "extraction_schema": schema("lesotho").model_dump(mode="json"),
            "agent": agent,
            "report": {"study_label": "Lesotho", "title": "The trial", "licence": "CC BY 4.0"},
        }
        # Through the service: the route also needs the report included at full text.
        request = RunRequest.model_validate(body | {"project_id": str(project)})
        return str(request_run(pg, commands, request, clock=ctx.clock).id)

    commands = Commands(ctx.require_ledger(), queue, PostgresReceipts(), ctx.clock)
    record_roles(pg, project, requester["id"], "Lead", ("lead",), event_id=new_id())
    lead = {"X-Kittum-Reviewer": str(requester["id"])}
    with TestClient(create_app(ctx), headers=lead) as client:
        janus_only = request(None)
        assert worker.run_once() is True
        done = client.get(f"{base}/{janus_only}").json()
        assert done["status"] == "succeeded"
        fields = client.get(f"{base}/{janus_only}/fields").json()
        paths = {p["path"] for f in fields for p in f["proposals"]}
        assert paths == {"janus"}
        assert any(p["status"] == "extracted" for f in fields for p in f["proposals"])

        # Asking for the agent without a model server says so in every LLM proposal.
        with_agent = request(AGENT.model_dump(mode="json"))
        assert worker.run_once() is True
        fields = client.get(f"{base}/{with_agent}/fields").json()
        llm = [p for f in fields for p in f["proposals"] if p["path"] == "llm"]
        assert llm and {p["status"] for p in llm} == {"error"}
        assert worker.run_once() is False  # nothing left behind in the queue
