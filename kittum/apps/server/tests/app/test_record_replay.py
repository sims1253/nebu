"""A live run recorded once and replayed offline, as a filmed demo would be.

The live run is configured as a deployment's would be: a personal contact address, an OpenAlex
key, and a hosted judge whose key comes from the environment. A lead builds a corpus from a
citation search, the worker enriches it from the sources and the judge screens it, with
`KITTUM_GATEWAY_RECORD` and `KITTUM_LLM_RECORD` set. The replay runs the same steps on a fresh
artifact store with `KITTUM_OFFLINE=1`, the recordings, and `KITTUM_LLM_REPLAY`, with no key, no
source, and no model server reachable. Its rows and ledger payloads are the live run's, except that
every model endpoint says it was replayed; and no recorded file holds a key or the address.

The live sources answer from `tests/enrich/fixtures/stack/`; nothing touches the network.
"""

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, quote_plus

import httpx2
import pytest
from psycopg import sql

from kittum.app import (
    AppContext,
    ConfigurationError,
    add_member,
    build_backends,
    build_gateway,
    build_judge,
    create_project,
    ingest_corpus,
    job_handlers,
    open_screening,
    publish_protocol,
    recording_secrets,
    request_enrichment,
)
from kittum.app.demo import DATA, PROTOCOL, SCOPE, SENTINELS
from kittum.core import SystemClock
from kittum.gateway import HttpRequest, RecordedTransport, RecordingRefused, RecordingTransport
from kittum.gateway.testing import FakeTransport
from kittum.ledger import PostgresLedger
from kittum.models import (
    CompletionRequest,
    LlmRecordingRefused,
    LlmRecordings,
    Message,
    ModelRegistry,
)
from kittum.runs import PostgresJobQueue, PostgresReceipts, Worker
from kittum.search import SearchPlan, import_file
from kittum.settings import LlmEndpoint, Settings
from kittum.storage import FilesystemStore
from tests.app.record_stack import STACK
from tests.app.test_hosted_model import RECIPES
from tests.enrich.support import CONTACT_EMAIL

if TYPE_CHECKING:
    from pathlib import Path

    from kittum.db import Database
    from kittum.gateway import HttpResponse

PERSONAL = "max.mustermann@uni-example.de"
OPENALEX_KEY = "oa-key-5f1c2d9e"
LLM_KEY = "sk-live-7d3a9b0c1e2f"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Hashes over a project's own record IDs, which differ between the two projects as the IDs do.
_ID_HASHES = re.compile(r'"(cache_key|records_hash|input_hash)": "[0-9a-f]{64}"')
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?")


class LiveSources:
    """The real sources, as recorded for the stack under the test address, asked with ours."""

    def __init__(self) -> None:
        self._recorded = RecordedTransport(STACK)
        self.sent: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.sent.append(request)
        url = request.url.replace(quote_plus(PERSONAL), quote_plus(CONTACT_EMAIL))
        return self._recorded.send(request.model_copy(update={"url": url}))


class HostedJudge:
    """A hosted model that finds every criterion met for a record about Lesotho, else unclear."""

    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        status = "met" if "Lesotho" in user else "unclear"
        answer = {
            c: {"quote": "", "rationale": f"Read as {status}.", "status": status}
            for c in body["response_format"]["json_schema"]["schema"]["properties"]
        }
        message = {"role": "assistant", "content": json.dumps(answer)}
        return httpx2.Response(
            200,
            json={
                "model": "example-model-2026-10",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            },
        )


def _run(database: Database, settings: Settings, artifacts: FilesystemStore, **live: Any) -> Any:
    """Build a project's corpus, enrich it, and screen it with the judge; return its rows and
    ledger payloads with IDs and times blanked out."""
    registry = ModelRegistry.load(local=settings.recipes_file)
    backends = build_backends(settings, registry, artifacts, client=live.get("client"))
    clock = SystemClock()
    ledger = PostgresLedger()
    ctx = AppContext(
        settings=settings,
        db=database,
        artifacts=artifacts,
        clock=clock,
        gateway=build_gateway(settings, database, artifacts, clock, transport=live.get("sources")),
        ledger=ledger,
        queue=PostgresJobQueue(),
        receipts=PostgresReceipts(),
        backends=backends,
        judge=build_judge(settings, registry, backends),
    )
    with database.transaction() as tx:
        pid, lead = create_project(tx, name="Record and replay", lead_name="Rec", ledger=ledger)
        for name, role in (
            ("Sam", "reviewer"),
            ("Jun", "reviewer"),
            ("Rita", "auditor"),
            ("Elena", "adjudicator"),
        ):
            add_member(tx, pid, display_name=name, roles=(role,), added_by=lead, ledger=ledger)
        publish_protocol(tx, pid, PROTOCOL.read_text(), approved_by=lead, ledger=ledger)
        import_file(
            tx,
            pid,
            SearchPlan(
                topic_id="TRIALS", source="citation_search", platform="citation", strategy="x"
            ),
            (DATA / "citation_search.ris").read_bytes(),
            "ris",
            ledger=ledger,
            actor_id=lead,
            artifacts=artifacts,
            clock=clock,
        )
        ingest_corpus(tx, pid, actor_id=lead, ledger=ledger)
        request_enrichment(
            tx,
            pid,
            actor_id=lead,
            trigger="corpus_built",
            ledger=ledger,
            queue=ctx.queue,  # type: ignore[arg-type]
            clock=clock,
        )
        open_screening(tx, ctx, pid, SCOPE, "title_abstract", actor_id=lead, sentinels=SENTINELS)
        # Other tests' leftover jobs are not this worker's to run.
        tx.execute(
            "update job set run_after = now() + interval '1 hour'"
            " where project_id <> %s and status = 'queued'",
            (pid,),
        )
    worker = Worker(database, PostgresJobQueue(), job_handlers(ctx), name="record-replay")
    while worker.run_once():
        pass

    with database.transaction() as tx:
        snapshot = {
            "ledger": [
                {"type": e.type, "payload": e.payload}
                for e in ledger.events(tx, pid)
                if not e.type.startswith(("project.", "protocol."))
            ],
            **{
                table: tx.execute(
                    sql.SQL("select * from {} where project_id = %s").format(sql.Identifier(table)),
                    (pid,),
                ).fetchall()
                for table in ("enrichment_run", "record_enrichment", "verdict")
            },
        }
    text = _UUID.sub("<id>", json.dumps(snapshot, default=str))
    return json.loads(_ID_HASHES.sub(r'"\1": "<hash>"', _TIME.sub("<time>", text)))


def _sorted(rows: list[Any]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@pytest.mark.postgres
def test_a_recorded_live_run_replays_offline_to_the_same_rows_and_ledger(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipes = tmp_path / "recipes.yaml"
    recipes.write_text(RECIPES)
    monkeypatch.setenv("EXAMPLE_LLM_KEY", LLM_KEY)
    gateway_dir, llm_dir = tmp_path / "gateway", tmp_path / "llm"
    live = Settings(
        database_url="unused",
        artifact_dir=tmp_path / "live-artifacts",
        contact_email=PERSONAL,
        openalex_api_key=OPENALEX_KEY,
        recipes_file=recipes,
        llm_endpoints=(
            LlmEndpoint(
                recipe_id="example-hosted",
                base_url="https://llm.example/v1",
                api_key_env="EXAMPLE_LLM_KEY",
            ),
        ),
        gateway_record=gateway_dir,
        llm_record=llm_dir,
    )
    sources, model = LiveSources(), HostedJudge()
    recorded = _run(
        database,
        live,
        FilesystemStore(live.artifact_dir),
        sources=sources,
        client=httpx2.Client(transport=httpx2.MockTransport(model)),
    )
    # The live run asked the sources with the address and the key, and the model with its key.
    assert any(quote_plus(PERSONAL) in r.url for r in sources.sent)
    assert any(("Authorization", f"Bearer {OPENALEX_KEY}") in r.headers for r in sources.sent)
    assert model.seen and all(r.headers["authorization"] == f"Bearer {LLM_KEY}" for r in model.seen)
    (run,) = recorded["enrichment_run"]
    assert run["finished_at"] == "<time>" and len(recorded["record_enrichment"]) == run["records"]
    assert any(r["retracted"] is not None for r in recorded["record_enrichment"])
    verdicts = recorded["verdict"]
    assert {v["excludes"] for v in verdicts} == {False}
    assert {c["status"] for v in verdicts for c in v["criteria"]} == {"met", "unclear"}

    # No recorded file holds a key or the contact address, in any spelling.
    written = [p for p in (*gateway_dir.rglob("*"), *llm_dir.rglob("*")) if p.is_file()]
    assert len(written) > len(model.seen)
    secrets = (LLM_KEY, OPENALEX_KEY, *{PERSONAL, quote_plus(PERSONAL), quote(PERSONAL, safe="")})
    for path in written:
        content = path.read_bytes()
        assert not any(s.encode() in content for s in secrets), path

    # The recordings and offline mode exclude each other's opposite.
    offline = replace(live, offline=True, gateway_recordings=gateway_dir)
    with pytest.raises(ConfigurationError, match="KITTUM_GATEWAY_RECORD"):
        build_gateway(offline, database, FilesystemStore(tmp_path / "x"), SystemClock())
    with pytest.raises(ConfigurationError, match="exclude each other"):
        build_backends(
            replace(live, llm_replay=llm_dir), ModelRegistry.load(), FilesystemStore(tmp_path / "x")
        )

    # The replay: offline, no key, nothing reachable, a fresh artifact store.
    monkeypatch.delenv("EXAMPLE_LLM_KEY")
    replay = replace(
        live,
        artifact_dir=tmp_path / "replay-artifacts",
        openalex_api_key=None,
        gateway_record=None,
        llm_record=None,
        offline=True,
        gateway_recordings=gateway_dir,
        llm_replay=llm_dir,
    )
    asked = len(model.seen)
    replayed = _run(database, replay, FilesystemStore(replay.artifact_dir))
    assert len(model.seen) == asked

    # Every model endpoint says it was replayed; with that marker taken off, nothing differs.
    endpoints = [a["endpoint"] for v in replayed["verdict"] for a in v["attempts"]] + [
        e["payload"]["endpoint"] for e in replayed["ledger"] if "endpoint" in e["payload"]
    ]
    assert endpoints and set(endpoints) == {"replay of https://llm.example/v1"}
    unmarked = json.loads(json.dumps(replayed).replace("replay of https://", "https://"))
    for table in ("ledger", "enrichment_run", "record_enrichment", "verdict"):
        assert _sorted(unmarked[table]) == _sorted(recorded[table]), table


def test_a_replay_judges_with_the_recipe_the_live_run_judged_with(tmp_path: Path) -> None:
    """Two models answered in the live run and the second judged. The replay, told nothing about
    the judge, must pick the second too, not whichever the recordings happen to list first; and a
    recording that does not say which one judged is refused rather than guessed."""
    path = tmp_path / "models"
    recipes = ("qwen3.8-27b-bf16", "gemma-4-31b-it-bf16")
    live = Settings(
        database_url="unused",
        artifact_dir=tmp_path / "artifacts",
        llm_endpoints=tuple(
            LlmEndpoint(recipe_id=r, base_url=f"http://gpu-{n}:8000/v1")
            for n, r in enumerate(recipes)
        ),
        llm_record=path,
    )
    registry = ModelRegistry.load()
    answer = {
        "model": "served",
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
    }
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _r: httpx2.Response(200, json=answer))
    )
    artifacts = FilesystemStore(live.artifact_dir)
    backends = build_backends(live, registry, artifacts, client=client)
    for recipe_id in recipes:
        message = Message(role="user", content=f"Read this as {recipe_id}.")
        backends[recipe_id].complete(CompletionRequest(model_id=recipe_id, messages=(message,)))
    # The judge is the recipe a replay would not pick by itself.
    first, judged = LlmRecordings(path).model_ids()
    judge = build_judge(replace(live, judge_model=judged), registry, backends)
    assert judge is not None and judge.recipe.id == judged

    replay = replace(live, llm_endpoints=(), llm_record=None, llm_replay=path)
    replays = build_backends(replay, registry, artifacts)
    assert set(replays) == {first, judged}
    replayed = build_judge(replay, registry, replays)
    assert replayed is not None and replayed.recipe.id == judged

    (path / "meta.json").unlink()
    with pytest.raises(ConfigurationError, match="does not say which recipe judged"):
        build_judge(replay, registry, replays)


def test_recording_takes_only_secrets_it_can_tell_from_ordinary_content(tmp_path: Path) -> None:
    """The dev database password `kittum` is also PubMed's `tool=kittum`: recording with it would
    refuse every PubMed request, so it refuses to start instead, as for any short or common secret
    or one inside what Kittum sends. With a long password (one that holds an `@`, percent-encoded
    in the DSN), PubMed is recorded, and the password itself is refused wherever it turns up."""
    store = FilesystemStore(tmp_path / "artifacts")
    base = Settings(
        database_url="postgresql://kittum:kittum@127.0.0.1:55432/kittum",
        artifact_dir=tmp_path / "artifacts",
        gateway_record=tmp_path / "sources",
    )
    for settings, name, problem in (
        (base, "the database password", "at least 12 characters"),
        (
            replace(base, database_url="postgresql://kittum:administrator@db/kittum"),
            "the database password",
            "not a common word",
        ),
        (
            replace(base, database_url="unused", openalex_api_key="example.invalid"),
            "KITTUM_OPENALEX_API_KEY",
            "part of what Kittum sends",
        ),
    ):
        with pytest.raises(ConfigurationError, match=f"recording needs {name}.*{problem}") as e:
            build_gateway(settings, None, store, SystemClock())  # type: ignore[arg-type]
        assert "administrator" not in str(e.value) and "example.invalid" not in str(e.value)
    with pytest.raises(ConfigurationError, match="recording needs the database password"):
        build_backends(
            replace(base, gateway_record=None, llm_record=tmp_path / "models"),
            ModelRegistry.load(),
            store,
        )

    password = "rec@rd-Only-7f3k"
    recipe_id = "qwen3.8-27b-bf16"
    settings = replace(
        base,
        database_url=f"postgresql://recorder:{quote(password, safe='')}@127.0.0.1:55432/kittum",
        llm_record=tmp_path / "models",
        llm_endpoints=(LlmEndpoint(recipe_id=recipe_id, base_url="http://gpu:8000/v1"),),
    )
    build_gateway(settings, None, store, SystemClock())  # type: ignore[arg-type]
    sources = RecordingTransport(
        FakeTransport(), tmp_path / "sources", secrets=recording_secrets(settings)
    )
    efetch = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    pubmed = HttpRequest(url=efetch, params=(("id", "32450107"), ("tool", "kittum")))
    assert sources.send(pubmed).status == 404  # recorded, whatever PubMed said
    with pytest.raises(RecordingRefused, match="parameter 'term'"):
        sources.send(HttpRequest(url=efetch, params=(("term", password),)))

    answer = {"model": "served", "choices": [{"message": {"content": "{}"}}]}
    client = httpx2.Client(
        transport=httpx2.MockTransport(lambda _r: httpx2.Response(200, json=answer))
    )
    backend = build_backends(settings, ModelRegistry.load(), store, client=client)[recipe_id]
    leaked = Message(role="user", content=f"The password is {password}.")
    with pytest.raises(LlmRecordingRefused, match="its request holds"):
        backend.complete(CompletionRequest(model_id=recipe_id, messages=(leaked,)))
    # The recordings, that is; the deployment's own artifact store keeps what it sent, as always.
    recorded = [p for d in ("sources", "models") for p in (tmp_path / d).rglob("*") if p.is_file()]
    written = b"".join(p.read_bytes() for p in recorded)
    assert b"tool=kittum" in written
    assert password.encode() not in written and quote(password, safe="").encode() not in written
