"""One judge call: build the request, retry malformed answers, verify every criterion. An answer
the token budget cut off is not retried: the same budget would cut it off again."""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from kittum.core import sha256_hex
from kittum.judge.prompt import answer_model, build_request
from kittum.judge.types import CriterionVerdict, Verdict
from kittum.judge.verify import injection_suspected, verify
from kittum.models import cut_off, split_reasoning

if TYPE_CHECKING:
    from pydantic import BaseModel

    from kittum.judge.prompt import CriterionAnswer
    from kittum.judge.types import JudgeConfig, ScreeningItem
    from kittum.models import Completion, CompletionRequest, LlmBackend
    from kittum.protocol import Criterion


@dataclass
class Attempt:
    request: CompletionRequest
    completion: Completion
    error: str | None  # why the answer was rejected, or None if it was valid


@dataclass
class JudgeRun:
    verdict: Verdict
    attempts: list[Attempt] = field(default_factory=list)


def applicable(
    criteria: tuple[Criterion, ...], scope_id: str, item: ScreeningItem
) -> tuple[Criterion, ...]:
    """The criteria that apply to the scope and can be decided at the item's stage."""
    return tuple(c for c in criteria if scope_id in c.applies_to and item.stage in c.decidable_at)


def _load_json(text: str) -> Any:
    _, answer = split_reasoning(text)
    try:
        return json.loads(answer)
    except ValueError:
        start = answer.find("{")
        if start == -1:
            raise
        value, end = json.JSONDecoder().raw_decode(answer, start)
        if answer[end:].strip():
            raise ValueError("text after the JSON object") from None
        return value


def parse_answer(
    text: str, criteria: tuple[Criterion, ...], model: type[BaseModel]
) -> dict[str, CriterionAnswer]:
    """The answer per criterion ID. Raises ValueError if the text does not fit the schema."""
    parsed = model.model_validate(_load_json(text))
    return {c.id: getattr(parsed, f"c{i}") for i, c in enumerate(criteria)}


def _invalid(criteria: tuple[Criterion, ...], rationale: str) -> tuple[CriterionVerdict, ...]:
    return tuple(
        CriterionVerdict(
            criterion_id=c.id,
            status="unclear",
            quote=None,
            rationale=rationale,
            quote_verified=False,
            failed_check="invalid_output",
        )
        for c in criteria
    )


def run_judge(
    item: ScreeningItem,
    scope_id: str,
    criteria: tuple[Criterion, ...],
    config: JudgeConfig,
    backend: LlmBackend,
) -> JudgeRun:
    chosen = applicable(criteria, scope_id, item)
    if not chosen:
        raise ValueError(f"no criterion applies to scope {scope_id} at stage {item.stage}")
    model = answer_model(chosen)
    attempts: list[Attempt] = []
    answers: dict[str, CriterionAnswer] | None = None
    stopped: str | None = None
    for n in range(config.max_attempts):
        request = build_request(item, chosen, config, attempt=n)
        completion = backend.complete(request)
        stopped = cut_off(request, completion)
        if stopped:
            attempts.append(Attempt(request, completion, stopped))
            break
        try:
            answers = parse_answer(completion.text, chosen, model)
        except (ValueError, ValidationError) as e:
            attempts.append(Attempt(request, completion, f"{type(e).__name__}: {e}"[:2000]))
            continue
        attempts.append(Attempt(request, completion, None))
        break
    last = attempts[-1].completion
    flags: list[str] = []
    if answers is None:
        flags.append("invalid_output")
        verdicts = _invalid(
            chosen, stopped or f"The model gave no valid answer in {len(attempts)} attempts."
        )
    else:
        injection = injection_suspected(item.source)
        if injection:
            flags.append("injection_suspected")
        verdicts = tuple(verify(c, answers[c.id], item.source, injection=injection) for c in chosen)
    verdict = Verdict(
        item_ref=item.item_ref,
        scope_id=scope_id,
        judge_config_id=config.id,
        criteria=verdicts,
        raw_output_hash=sha256_hex(last.text.encode()),
        model_identity=last.model_identity,
        flags=tuple(flags),
    )
    return JudgeRun(verdict, attempts)
