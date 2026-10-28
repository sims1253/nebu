"""A review screened by a hosted model, configured as a deployment would configure one.

The recipe comes from the deployment's own recipes file, the endpoint and the name of the
variable that holds its key from the environment. The key goes out with every request and
nowhere else: not into the stored request bytes, the ledger, any table, the reports, or the log.
What the disclosure needs instead is recorded: the recipe, its serving, and the endpoint.
"""

import json
import logging
from typing import TYPE_CHECKING

import httpx2
import pytest
from psycopg import sql

from kittum.app import (
    AppContext,
    add_member,
    build_judge,
    create_project,
    ingest_corpus,
    job_handlers,
    open_screening,
    publish_protocol,
)
from kittum.app.demo import DATA, PROTOCOL, SCOPE, SENTINELS
from kittum.core import new_id
from kittum.judge import VERDICT_RECORDED
from kittum.ledger import PostgresLedger
from kittum.models import ModelRegistry, backends_from_endpoints
from kittum.report import load_review, render_files
from kittum.runs import PostgresJobQueue, Worker
from kittum.search import SearchPlan, import_file
from kittum.settings import Settings
from kittum.storage import FilesystemStore

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Database

pytestmark = pytest.mark.postgres

RECIPES = """
recipes:
  - id: example-hosted
    family: example
    repository: example-org/example-model
    served_name: example-model
    revision: "2026-10"
    weights_licence: mit
    licence_url: https://example.org/LICENSE
    serving: hosted
    context_tokens: 131072
    reasoning: true
    extra_body: {"thinking": {"type": "disabled"}}
"""


class HostedApi:
    """A hosted reasoning model that thinks anyway and finds every criterion unclear."""

    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        body = json.loads(request.content)
        criteria = body["response_format"]["json_schema"]["schema"]["properties"]
        answer = {
            c: {"quote": "", "rationale": "The abstract does not say.", "status": "unclear"}
            for c in criteria
        }
        message = {
            "role": "assistant",
            "content": "<think>Reading the abstract.</think>\n" + json.dumps(answer),
            "reasoning_content": "Reading the abstract.",
        }
        return httpx2.Response(
            200,
            json={
                "model": "example-model-2026-10",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )


def test_a_hosted_judge_gets_its_key_from_the_environment_and_records_none_of_it(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    key = f"sk-test-{new_id().hex}"
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(RECIPES)
    monkeypatch.setenv("KITTUM_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("KITTUM_RECIPES_FILE", str(recipes))
    monkeypatch.setenv("KITTUM_LLM_ENDPOINTS", "example-hosted=https://llm.example/v1")
    monkeypatch.setenv("KITTUM_LLM_API_KEYS", "example-hosted=EXAMPLE_LLM_KEY")
    monkeypatch.setenv("EXAMPLE_LLM_KEY", key)
    caplog.set_level(logging.DEBUG)

    # What `AppContext.from_settings` does, with the HTTP client swapped for the fake API.
    settings = Settings.from_env()
    artifacts = FilesystemStore(settings.artifact_dir)
    registry = ModelRegistry.load(local=settings.recipes_file)
    api = HostedApi()
    backends = backends_from_endpoints(
        settings.llm_endpoints,
        registry,
        artifacts,
        client=httpx2.Client(transport=httpx2.MockTransport(api)),
    )
    judge = build_judge(settings, registry, backends)
    assert judge is not None and judge.recipe.serving == "hosted"
    ledger = PostgresLedger()
    ctx = AppContext(
        settings=settings,
        db=database,
        artifacts=artifacts,
        ledger=ledger,
        backends=backends,
        judge=judge,
    )

    with database.transaction() as tx:
        pid, lead = create_project(tx, name="Hosted judge", lead_name="Morgan Lee", ledger=ledger)
        for name, role in (
            ("Sam Okafor", "reviewer"),
            ("Jun Park", "reviewer"),
            ("Rita Nunes", "auditor"),
            ("Elena Weiss", "adjudicator"),
        ):
            add_member(tx, pid, display_name=name, roles=(role,), added_by=lead, ledger=ledger)
        publish_protocol(tx, pid, PROTOCOL.read_text(), approved_by=lead, ledger=ledger)
        import_file(
            tx,
            pid,
            SearchPlan(topic_id="TRIALS", source="central", platform="CENTRAL", strategy="x"),
            (DATA / "central_export.ris").read_bytes(),
            "ris",
            ledger=ledger,
            actor_id=lead,
            artifacts=artifacts,
            clock=ctx.clock,
        )
        ingest_corpus(tx, pid, actor_id=lead, ledger=ledger)
        open_screening(tx, ctx, pid, SCOPE, "title_abstract", actor_id=lead, sentinels=SENTINELS)
        # Other tests' leftover jobs are not this worker's to run.
        tx.execute(
            "update job set run_after = now() + interval '1 hour'"
            " where project_id <> %s and status = 'queued'",
            (pid,),
        )
    worker = Worker(database, PostgresJobQueue(), job_handlers(ctx), name="hosted-test")
    while worker.run_once():
        pass

    # Every request carried the key, and the recipe's thinking switch went out unchanged.
    assert len(api.seen) == 8  # seven records and one sentinel
    for request in api.seen:
        assert request.headers["authorization"] == f"Bearer {key}"
        assert str(request.url) == "https://llm.example/v1/chat/completions"
        assert json.loads(request.content)["thinking"] == {"type": "disabled"}

    with database.transaction() as tx:
        events = ledger.events(tx, pid)
        verdicts = tx.execute("select * from verdict where project_id = %s", (pid,)).fetchall()
        recipe = tx.execute(
            "select model_recipe from judge_config where id = %s", (judge.config.id,)
        ).fetchone()
        review = load_review(tx, pid)
        # The key is in no row of any table, the ledger and the job queue included.
        tables = tx.execute(
            "select table_name from information_schema.tables"
            " where table_schema = 'public' and table_type = 'BASE TABLE'"
        ).fetchall()
        for table in tables:
            leaked = tx.execute(
                sql.SQL("select count(*) as n from {} t where row_to_json(t)::text like %s").format(
                    sql.Identifier(table["table_name"])
                ),
                (f"%{key}%",),
            ).fetchone()
            assert leaked is not None and leaked["n"] == 0, table["table_name"]

    # The stored request bytes are exactly what was sent, without the key (it is a header).
    assert len(verdicts) == 8
    sent = {r.content for r in api.seen}
    for verdict in verdicts:
        (attempt,) = verdict["attempts"]
        assert artifacts.get(attempt["raw_request_hash"]) in sent
        assert attempt["endpoint"] == "https://llm.example/v1"
        assert verdict["model_identity"] == "example-model-2026-10"
        assert all(c["status"] == "unclear" for c in verdict["criteria"])  # thinking stripped
    stored = [p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()]
    assert stored and not any(key.encode() in p.read_bytes() for p in stored)
    assert key not in caplog.text

    # The ledger and the disclosure say where the records went.
    recorded = [e.payload for e in events if e.type == VERDICT_RECORDED]
    assert len(recorded) == 8
    assert {p["endpoint"] for p in recorded} == {"https://llm.example/v1"}
    assert recipe is not None and recipe["model_recipe"]["serving"] == "hosted"
    files = {f.path: f.data for f in render_files(review, "ai_disclosure")}
    assert key.encode() not in b"".join(files.values())
    (model,) = json.loads(files["ai_disclosure.json"])["models"]
    assert (model["recipe_id"], model["serving"], model["endpoints"]) == (
        "example-hosted",
        "hosted",
        ["https://llm.example/v1"],
    )
    assert (
        "example-model-2026-10 (hosted at https://llm.example/v1)"
        in files["ai_disclosure.md"].decode()
    )


def test_a_key_pasted_where_its_variable_name_belongs_is_refused_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KITTUM_LLM_API_KEYS names variables. A key pasted there by mistake stops the process at
    startup, and the error does not repeat it, so it never reaches a job error or a log."""
    monkeypatch.setenv("KITTUM_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("KITTUM_LLM_ENDPOINTS", "example-hosted=https://llm.example/v1")
    pasted = "sk-live-4f9c2e1d8b7a"
    monkeypatch.setenv("KITTUM_LLM_API_KEYS", f"example-hosted={pasted}")
    with pytest.raises(ValueError, match="must be the name of an environment variable") as e:
        Settings.from_env()
    assert pasted not in str(e.value)

    # A key variable for a recipe no endpoint serves would never be read: refused too.
    monkeypatch.setenv("KITTUM_LLM_API_KEYS", "other-recipe=OTHER_KEY")
    with pytest.raises(ValueError, match="'other-recipe', which no KITTUM_LLM_ENDPOINTS entry"):
        Settings.from_env()

    monkeypatch.setenv("KITTUM_LLM_API_KEYS", "example-hosted=EXAMPLE_LLM_KEY")
    (endpoint,) = Settings.from_env().llm_endpoints
    assert endpoint.api_key_env == "EXAMPLE_LLM_KEY"
