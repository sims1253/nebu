"""`judge_many` against Postgres: verdict rows, raw outputs, ledger events, and the cache."""

import json
import os
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from psycopg.rows import dict_row

from kittum.core import FixedClock
from kittum.judge import (
    VERDICT_RECORDED,
    JudgeConfigConflict,
    judge_many,
    load_verdicts,
    register_config,
)
from kittum.ledger.testing import InMemoryLedger
from kittum.models import ModelRegistry
from kittum.models.testing import RecordedBackend
from kittum.storage import MemoryStore
from tests.judge.records import (
    ADOLESCENTS,
    CHEMO_NAIVE,
    CONFIG,
    CRITERIA,
    EXCLUDED_CHEMO_NAIVE,
    INJECTED,
    SCOPE,
    recorded,
)

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.judge import JudgeConfig
    from kittum.models.testing import RecordedBackend as Backend

pytestmark = pytest.mark.postgres

PROTOCOL_HASH = "a" * 64
ITEMS = [ADOLESCENTS, EXCLUDED_CHEMO_NAIVE, CHEMO_NAIVE, INJECTED]
RECIPE = ModelRegistry.load().get(CONFIG.model_id)


def _seed(pg: Tx) -> tuple[Any, Any]:
    project = pg.execute(
        "insert into project (name, policy_version) values ('NSCLC review', 'p1') returning id"
    ).fetchone()
    actor = pg.execute(
        "insert into actor (identity_ref, kind) values ('model:qwen3.8-27b-bf16', 'model')"
        " on conflict (identity_ref) do update set kind = excluded.kind returning id"
    ).fetchone()
    assert project and actor
    return project["id"], actor["id"]


def _run(
    pg: Tx,
    backend: Backend,
    ledger: InMemoryLedger,
    store: MemoryStore,
    ids: tuple[Any, Any],
    config: JudgeConfig = CONFIG,
) -> list[Any]:
    return judge_many(
        pg,
        ITEMS,
        project_id=ids[0],
        actor_id=ids[1],
        scope_id=SCOPE,
        criteria=CRITERIA,
        protocol_hash=PROTOCOL_HASH,
        config=config,
        recipe=RECIPE,
        backend=backend,
        ledger=ledger,
        store=store,
        clock=FixedClock(datetime(2026, 10, 28, 12, tzinfo=UTC)),
        concurrency=3,
    )


def test_a_batch_records_verdicts_and_a_rerun_makes_no_model_calls(pg: Tx) -> None:
    ids = _seed(pg)
    ledger, store = InMemoryLedger(), MemoryStore()
    backend = recorded(*ITEMS)

    verdicts = _run(pg, backend, ledger, store, ids)

    assert len(backend.requests) == 4
    assert [v.item_ref for v in verdicts] == [i.item_ref for i in ITEMS]
    assert [v.excludes for v in verdicts] == [True, False, True, False]

    rows = pg.execute(
        "select v.*, c.weights_hash, c.prompt_version from verdict v"
        " join judge_config c on c.id = v.judge_config_id"
        " where project_id = %s order by item_ref",
        (ids[0],),
    ).fetchall()
    assert {r["item_ref"]: r["excludes"] for r in rows} == {
        i.item_ref: v.excludes for i, v in zip(ITEMS, verdicts, strict=True)
    }
    injected = next(r for r in rows if r["item_ref"] == INJECTED.item_ref)
    assert injected["flags"] == ["injection_suspected"]
    assert injected["model_identity"] == "Qwen/Qwen3.8-27B"
    assert injected["prompt_version"] == "judge.v1"
    negated = next(r for r in rows if r["item_ref"] == EXCLUDED_CHEMO_NAIVE.item_ref)
    chemo = next(c for c in negated["criteria"] if c["criterion_id"] == "C_PRIOR_CHEMO")
    assert (chemo["status"], chemo["downgraded_from"], chemo["failed_check"]) == (
        "unclear",
        "not_met",
        "negation",
    )
    titles = {i.item_ref: i.title for i in ITEMS}
    # Raw output and the rendered request are in the store, so the verdict can be replayed.
    for row in rows:
        (attempt,) = row["attempts"]
        assert store.get(row["raw_output_hash"]) == store.get(attempt["output_hash"])
        rendered = json.loads(store.get(row["rendered_prompt_hash"]))
        assert titles[row["item_ref"]] in rendered["messages"][1]["content"]
        assert (rendered["temperature"], rendered["seed"]) == (0, 0)

    events = ledger.events(pg, ids[0])
    assert [e.type for e in events] == [VERDICT_RECORDED] * 4
    assert {e.payload["item_ref"]: e.payload["excludes"] for e in events} == {
        r["item_ref"]: r["excludes"] for r in rows
    }
    assert ledger.verify(pg, ids[0]).ok

    silent = RecordedBackend()  # raises if anything asks it
    again = _run(pg, silent, ledger, store, ids)
    assert silent.requests == []
    assert again == verdicts
    assert len(ledger.events(pg, ids[0])) == 4
    assert load_verdicts(pg, ids[0], scope_id=SCOPE, item_refs=[CHEMO_NAIVE.item_ref]) == [
        verdicts[2]
    ]


def test_a_new_seed_is_a_new_cache_key_and_a_reused_config_id_is_refused(pg: Tx) -> None:
    ids = _seed(pg)
    ledger, store = InMemoryLedger(), MemoryStore()
    _run(pg, recorded(*ITEMS), ledger, store, ids)

    with pytest.raises(JudgeConfigConflict, match="decoding"):
        _run(pg, RecordedBackend(), ledger, store, ids, CONFIG.model_copy(update={"seed": 5}))

    reseeded = CONFIG.model_copy(update={"id": "qwen-ta-v1-seed5", "seed": 5})
    backend = RecordedBackend(default='{"not": "valid"}')
    verdicts = _run(pg, backend, ledger, store, ids, reseeded)
    assert len(backend.requests) == 8  # four records, two attempts each
    assert all(v.flags == ("invalid_output",) for v in verdicts)
    assert not any(v.excludes for v in verdicts)
    row = pg.execute(
        "select attempts from verdict where judge_config_id = %s and item_ref = %s",
        (reseeded.id, ADOLESCENTS.item_ref),
    ).fetchone()
    assert row is not None
    assert [a["seed"] for a in row["attempts"]] == [5, 6]
    assert all(a["error"] for a in row["attempts"])


def test_verdict_rows_cannot_be_changed(pg: Tx) -> None:
    ids = _seed(pg)
    _run(pg, recorded(*ITEMS), InMemoryLedger(), MemoryStore(), ids)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        pg.execute("update verdict set excludes = true where project_id = %s", (ids[0],))


@pytest.mark.usefixtures("pg")  # skips without a database
def test_concurrent_batches_record_each_verdict_once() -> None:
    """Two workers miss the cache together; the loser takes the winner's row and stays usable."""
    url = os.environ["KITTUM_DATABASE_URL"]
    config = CONFIG.model_copy(update={"id": f"race-{time.time_ns()}"})
    connect = lambda: psycopg.Connection[dict[str, Any]].connect(url, row_factory=dict_row)  # noqa: E731
    with connect() as first, connect() as second, connect() as watcher:
        watcher.autocommit = True
        ids = _seed(first)
        register_config(first, config, RECIPE, FixedClock(datetime(2026, 10, 28, tzinfo=UTC)))
        first.commit()
        try:
            winner_ledger, loser_ledger = InMemoryLedger(), InMemoryLedger()
            winner = _run(first, recorded(*ITEMS), winner_ledger, MemoryStore(), ids, config)
            # `first` holds its uncommitted rows; `second` misses the cache and blocks on insert.
            pid = second.execute("select pg_backend_pid() as pid").fetchone()["pid"]
            result: dict[str, Any] = {}

            def race() -> None:
                try:
                    backend = recorded(*ITEMS)
                    result["verdicts"] = _run(
                        second, backend, loser_ledger, MemoryStore(), ids, config
                    )
                    result["calls"] = len(backend.requests)
                    second.commit()
                except Exception as e:
                    result["error"] = e
                    second.rollback()

            thread = threading.Thread(target=race)
            thread.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and thread.is_alive():
                row = watcher.execute(
                    "select cardinality(pg_blocking_pids(%s)) as n", (pid,)
                ).fetchone()
                if row and row["n"]:
                    break
                time.sleep(0.02)
            first.commit()
            thread.join(timeout=10)

            assert "error" not in result, result.get("error")
            assert result["calls"] == len(ITEMS)  # it did miss the cache
            assert result["verdicts"] == winner
            assert len(winner_ledger.events(first, ids[0])) == len(ITEMS)
            assert loser_ledger.events(second, ids[0]) == []
            count = watcher.execute(
                "select count(*) as n from verdict where judge_config_id = %s", (config.id,)
            ).fetchone()
            assert count == {"n": len(ITEMS)}
        finally:
            first.rollback()
            watcher.execute("delete from verdict where judge_config_id = %s", (config.id,))
            watcher.execute("delete from judge_config where id = %s", (config.id,))
            watcher.execute("delete from project where id = %s", (ids[0],))
