"""Recording verdicts, and running a batch that skips what is already recorded."""

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from kittum.core import KittumError, canonical_json, content_hash, new_id
from kittum.judge.prompt import (
    PROMPT_VERSION,
    build_request,
    prompt_template_hash,
    schema_template_hash,
)
from kittum.judge.run import applicable, run_judge
from kittum.judge.types import CriterionVerdict, Verdict
from kittum.ledger import EventDraft
from kittum.models import weights_hash

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kittum.core import Clock, Id
    from kittum.db import Tx
    from kittum.judge.run import JudgeRun
    from kittum.judge.types import JudgeConfig, ScreeningItem
    from kittum.ledger import Ledger
    from kittum.models import LlmBackend, ModelRecipe
    from kittum.protocol import Criterion
    from kittum.storage import ArtifactStore

STAGE_VERSION = "judge.stage.v1"
VERDICT_RECORDED = "judge.verdict_recorded"


class JudgeConfigConflict(KittumError):
    """A judge config ID is already registered with a different model, prompt, or decoding."""


def decoding(config: JudgeConfig, recipe: ModelRecipe) -> dict[str, Any]:
    return {
        "temperature": config.temperature,
        "seed": config.seed,
        "max_tokens": config.max_tokens,
        "max_attempts": config.max_attempts,
        "extra_body": recipe.extra_body,
    }


def _check_recipe(config: JudgeConfig, recipe: ModelRecipe) -> None:
    if recipe.id != config.model_id:
        raise ValueError(f"judge config {config.id} uses {config.model_id}, not {recipe.id}")


def register_config(tx: Tx, config: JudgeConfig, recipe: ModelRecipe, clock: Clock) -> None:
    """Record a judge config once. Reusing its ID for anything else raises."""
    _check_recipe(config, recipe)
    if config.prompt_version != PROMPT_VERSION:
        raise ValueError(f"unknown prompt version {config.prompt_version!r}")
    values = {
        "model_recipe_id": recipe.id,
        "weights_hash": weights_hash(recipe),
        "prompt_version": config.prompt_version,
        "prompt_hash": prompt_template_hash(),
        "schema_hash": schema_template_hash(),
        "decoding": decoding(config, recipe),
    }
    tx.execute(
        "insert into judge_config (id, model_recipe_id, model_recipe, weights_hash,"
        " prompt_version, prompt_hash, schema_hash, decoding, created_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (id) do nothing",
        (
            config.id,
            values["model_recipe_id"],
            Jsonb(recipe.model_dump(mode="json")),
            values["weights_hash"],
            values["prompt_version"],
            values["prompt_hash"],
            values["schema_hash"],
            Jsonb(values["decoding"]),
            clock.now(),
        ),
    )
    row = tx.execute(
        "select model_recipe_id, weights_hash, prompt_version, prompt_hash, schema_hash, decoding"
        " from judge_config where id = %s",
        (config.id,),
    ).fetchone()
    assert row is not None
    changed = sorted(k for k, v in values.items() if row[k] != v)
    if changed:
        raise JudgeConfigConflict(
            f"judge config {config.id} exists with other {', '.join(changed)}"
        )


def cache_key(
    item: ScreeningItem,
    scope_id: str,
    criteria: tuple[Criterion, ...],
    config: JudgeConfig,
    recipe: ModelRecipe,
    protocol_hash: str,
) -> str:
    """Stage version, input, protocol, rendered prompt, weights, decoding, and schema."""
    request = build_request(item, applicable(criteria, scope_id, item), config)
    return content_hash(
        {
            "stage_version": STAGE_VERSION,
            "input_hash": content_hash(item, domain="kittum.screening_item.v1"),
            "scope_id": scope_id,
            "protocol_hash": protocol_hash,
            "prompt_hash": content_hash(
                [m.model_dump() for m in request.messages], domain="kittum.prompt.v1"
            ),
            "weights_hash": weights_hash(recipe),
            "decoding": decoding(config, recipe),
            "schema_hash": content_hash(request.json_schema, domain="kittum.json_schema.v1"),
        },
        domain="kittum.judge_cache.v1",
    )


def _verdict_from_row(row: dict[str, Any]) -> Verdict:
    return Verdict(
        item_ref=row["item_ref"],
        scope_id=row["scope_id"],
        judge_config_id=row["judge_config_id"],
        criteria=tuple(CriterionVerdict.model_validate(c) for c in row["criteria"]),
        raw_output_hash=row["raw_output_hash"],
        model_identity=row["model_identity"],
        flags=tuple(row["flags"]),
    )


def load_verdicts(
    tx: Tx, project_id: Id, *, scope_id: str | None = None, item_refs: Sequence[str] | None = None
) -> list[Verdict]:
    rows = tx.execute(
        "select * from verdict where project_id = %s"
        " and (%s::text is null or scope_id = %s)"
        " and (%s::text[] is null or item_ref = any(%s))"
        " order by created_at, id",
        (project_id, scope_id, scope_id, item_refs, item_refs),
    ).fetchall()
    return [_verdict_from_row(r) for r in rows]


def _record(
    tx: Tx,
    run: JudgeRun,
    item: ScreeningItem,
    key: str,
    *,
    project_id: Id,
    actor_id: Id,
    protocol_hash: str,
    ledger: Ledger,
    store: ArtifactStore,
    clock: Clock,
) -> Verdict:
    """Insert the verdict and its event, or return the verdict another worker recorded first."""
    verdict = run.verdict
    attempts = []
    for a in run.attempts:
        attempts.append(
            {
                "seed": a.request.seed,
                "request_hash": a.completion.request_hash,
                "rendered_request": store.put(canonical_json(a.request)),
                "raw_request_hash": a.completion.raw_request_hash,
                "raw_response_hash": a.completion.raw_response_hash,
                "output_hash": store.put(a.completion.text.encode()),
                "model_identity": a.completion.model_identity,
                "finish_reason": a.completion.finish_reason,
                "error": a.error,
            }
        )
    first = run.attempts[0].request
    verdict_id = new_id()
    criteria = [c.model_dump(mode="json") for c in verdict.criteria]
    inserted = tx.execute(
        "insert into verdict (id, project_id, judge_config_id, cache_key, item_ref, scope_id,"
        " stage, input_hash, protocol_hash, rendered_prompt_hash, schema_hash, criteria,"
        " excludes, flags, model_identity, raw_output_hash, attempts, created_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (project_id, cache_key) do nothing returning id",
        (
            verdict_id,
            project_id,
            verdict.judge_config_id,
            key,
            verdict.item_ref,
            verdict.scope_id,
            item.stage,
            content_hash(item, domain="kittum.screening_item.v1"),
            protocol_hash,
            attempts[0]["rendered_request"],
            content_hash(first.json_schema, domain="kittum.json_schema.v1"),
            Jsonb(criteria),
            verdict.excludes,
            list(verdict.flags),
            verdict.model_identity,
            verdict.raw_output_hash,
            Jsonb(attempts),
            clock.now(),
        ),
    ).fetchone()
    if inserted is None:
        winner = tx.execute(
            "select * from verdict where project_id = %s and cache_key = %s", (project_id, key)
        ).fetchone()
        assert winner is not None
        return _verdict_from_row(winner)
    ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type=VERDICT_RECORDED,
            schema_version=1,
            actor_id=actor_id,
            payload={
                "verdict_id": str(verdict_id),
                "item_ref": verdict.item_ref,
                "scope_id": verdict.scope_id,
                "stage": item.stage,
                "judge_config_id": verdict.judge_config_id,
                "cache_key": key,
                "excludes": verdict.excludes,
                "statuses": {c.criterion_id: c.status for c in verdict.criteria},
                "flags": list(verdict.flags),
                "model_identity": verdict.model_identity,
                "raw_output_hash": verdict.raw_output_hash,
                "rendered_prompt_hash": attempts[0]["rendered_request"],
            },
        ),
    )
    return verdict


def judge_many(
    tx: Tx,
    items: Sequence[ScreeningItem],
    *,
    project_id: Id,
    actor_id: Id,
    scope_id: str,
    criteria: tuple[Criterion, ...],
    protocol_hash: str,
    config: JudgeConfig,
    recipe: ModelRecipe,
    backend: LlmBackend,
    ledger: Ledger,
    store: ArtifactStore,
    clock: Clock,
    concurrency: int = 4,
) -> list[Verdict]:
    """Judge every item once, in input order, at most `concurrency` model calls at a time.

    A verdict already recorded under the same cache key is returned without a model call, so a
    rerun of a finished batch costs nothing. If a concurrent batch records the same key first,
    its verdict is returned and no second row or event is written. New verdicts, their artifacts, and one
    `judge.verdict_recorded` event each are written through `tx`.
    """
    _check_recipe(config, recipe)
    register_config(tx, config, recipe, clock)
    keys = [cache_key(i, scope_id, criteria, config, recipe, protocol_hash) for i in items]
    rows = tx.execute(
        "select * from verdict where project_id = %s and cache_key = any(%s)",
        (project_id, list(set(keys))),
    ).fetchall()
    done = {r["cache_key"]: _verdict_from_row(r) for r in rows}
    todo = {k: item for k, item in zip(keys, items, strict=True) if k not in done}
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            runs = list(
                pool.map(
                    lambda item: run_judge(item, scope_id, criteria, config, backend),
                    todo.values(),
                )
            )
        for (key, item), run in zip(todo.items(), runs, strict=True):
            done[key] = _record(
                tx,
                run,
                item,
                key,
                project_id=project_id,
                actor_id=actor_id,
                protocol_hash=protocol_hash,
                ledger=ledger,
                store=store,
                clock=clock,
            )
    return [done[k] for k in keys]
