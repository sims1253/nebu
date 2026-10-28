"""Ask a model for a draft or an improvement, and check every answer the way a hand-written
protocol is checked (`kittum.protocol.compile`). An answer with problems goes back to the model
with each problem and its path, at most `REPAIRS` times. Nothing here touches the database."""

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError, create_model

from kittum.assist import prompts
from kittum.assist.model import (
    Answer,
    ModelAttempt,
    ModelUse,
    PartKind,
    SuggestedChange,
    Suggestion,
)
from kittum.assist.paths import get, replace
from kittum.assist.schema import from_strict, strict_schema
from kittum.core import Record
from kittum.models import BackendError, CompletionRequest, Message, request_key
from kittum.protocol import (
    Criterion,
    ProtocolError,
    ProtocolIssue,
    ReviewProtocol,
    ReviewQuestion,
    compile,
    path_of,
    strategy_problems,
)

if TYPE_CHECKING:
    from kittum.core import Id
    from kittum.models import Completion, LlmBackend

REPAIRS = 2
DRAFT_TOKENS = 12000
IMPROVE_TOKENS = 4000


class StrategyEdit(Record):
    find: str
    replace: str
    rationale: str


class StrategyAnswer(Record):
    changes: tuple[StrategyEdit, ...]


class FieldNote(Record):
    field: str
    rationale: str


PART_MODELS: dict[PartKind, type[Record]] = {"question": ReviewQuestion, "criterion": Criterion}
_FIXED_FIELDS = frozenset({"id", "extensions"})


def part_answer(model: type[Record]) -> type[Record]:
    """The answer to an improvement of a part of type `model`: the whole part, and why."""
    return create_model(
        f"{model.__name__}Answer",
        __base__=Record,
        replacement=(model, ...),
        changes=(tuple[FieldNote, ...], ...),
    )


ANSWER_MODELS: dict[PartKind, type[Record]] = {
    "strategy": StrategyAnswer,
    **{kind: part_answer(model) for kind, model in PART_MODELS.items()},
}

_PARTS = (
    (re.compile(r"^questions\[[^\]]+\]$"), "question"),
    (re.compile(r"^search_topics\[[^\]]+\]\.criteria\[[^\]]+\]$"), "criterion"),
    (
        re.compile(r"^search_topics\[[^\]]+\]\.source_plan\.sources\[[^\]]+\]\.strategy$"),
        "strategy",
    ),
)


def part_kind(part: str) -> PartKind | None:
    return next((kind for pattern, kind in _PARTS if pattern.match(part)), None)


type Check = Callable[[Any], tuple[Any, list[ProtocolIssue]]]


def _issue(path: str, message: str) -> ProtocolIssue:
    return ProtocolIssue(path=path, message=message)


def _parse(text: str) -> tuple[Any, list[ProtocolIssue]]:
    body = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.DOTALL)
    try:
        data = json.loads(fenced.group(1) if fenced else body)
    except ValueError as e:
        return None, [_issue("", f"the answer is not JSON: {e}")]
    if not isinstance(data, dict):
        return None, [_issue("", "the answer must be one JSON object")]
    return data, []


def _pydantic_issues(e: ValidationError, prefix: tuple[str | int, ...]) -> list[ProtocolIssue]:
    return [
        _issue(
            path_of(*prefix, *(p for p in err["loc"] if not str(p).startswith("function-"))),
            err["msg"],
        )
        for err in e.errors()
    ]


def _converse(
    backend: LlmBackend,
    recipe_id: str,
    *,
    purpose: str,
    instructions: str,
    request: str,
    answer_model: type[Record],
    check: Check,
    max_tokens: int,
) -> tuple[Any, ModelUse, list[ProtocolIssue], str | None]:
    """Ask, check, and send the problems back until an answer passes or the rounds run out.
    Returns the checked result (None if none passed), the model use, the last problems, and the
    last raw answer."""
    schema = strict_schema(answer_model)
    messages = [Message(role="system", content=instructions), Message(role="user", content=request)]
    attempts: list[ModelAttempt] = []
    last: Completion | None = None
    issues: list[ProtocolIssue] = []
    result: Any = None
    for _ in range(1 + REPAIRS):
        call = CompletionRequest(
            model_id=recipe_id, messages=tuple(messages), json_schema=schema, max_tokens=max_tokens
        )
        try:
            completion = backend.complete(call)
        except BackendError as e:
            issues = [_issue("", f"the model server failed: {e}")]
            attempts.append(
                ModelAttempt(
                    request_hash=request_key(call),
                    raw_request_hash=e.raw_request_hash,
                    raw_response_hash=e.raw_response_hash,
                    model_identity=None,
                    issues=tuple(issues),
                )
            )
            break
        last = completion
        data, issues = _parse(completion.text)
        if not issues:
            converted, problems = from_strict(data, answer_model)
            issues = [_issue(path, message) for path, message in problems]
            if not issues:
                result, issues = check(converted)
        attempts.append(
            ModelAttempt(
                request_hash=completion.request_hash,
                raw_request_hash=completion.raw_request_hash,
                raw_response_hash=completion.raw_response_hash,
                model_identity=completion.model_identity,
                finish_reason=completion.finish_reason,
                issues=tuple(issues),
            )
        )
        if not issues:
            break
        result = None
        messages += [
            Message(role="assistant", content=completion.text),
            Message(role="user", content=prompts.problems([(i.path, i.message) for i in issues])),
        ]
    use = ModelUse(
        purpose="draft" if purpose == "draft" else "improve",
        recipe_id=recipe_id,
        model_identity=last.model_identity if last else None,
        serving=last.serving if last else None,
        endpoint=last.endpoint if last else None,
        prompt_version=prompts.PROMPT_VERSION,
        prompt_hash=prompts.instructions_hash(instructions),
        attempts=tuple(attempts),
    )
    return result, use, issues, last.text if last else None


def _compiled(document: dict[str, Any]) -> tuple[dict[str, Any] | None, list[ProtocolIssue]]:
    try:
        version = compile(document)
    except ProtocolError as e:
        return None, list(e.issues)
    return version.protocol.model_dump(mode="json"), []


def normal(document: dict[str, Any]) -> dict[str, Any]:
    """A stored document with its fields in the protocol's order, whatever order storage kept."""
    return ReviewProtocol.model_validate(document).model_dump(mode="json")


def draft(backend: LlmBackend, recipe_id: str, description: str, language: str = "en") -> Answer:
    """A first protocol from the lead's description of the review."""

    def check(data: Any) -> tuple[Any, list[ProtocolIssue]]:
        # The same path a hand-written source takes: schema, cross-checks, profile rules.
        return _compiled(data)

    document, use, issues, raw = _converse(
        backend,
        recipe_id,
        purpose="draft",
        instructions=prompts.DRAFT_INSTRUCTIONS,
        request=prompts.draft_request(description, language),
        answer_model=ReviewProtocol,
        check=check,
        max_tokens=DRAFT_TOKENS,
    )
    return Answer(model=use, document=document, issues=tuple(issues), raw_answer=raw)


def improve(
    backend: LlmBackend, recipe_id: str, base_id: Id, document: dict[str, Any], part: str
) -> Answer:
    """Changes to one part of `document` (a question, a criterion, or a source's strategy), each
    with its rationale. `document` compiles; so does it with every change applied."""
    kind = part_kind(part)
    if kind is None:
        raise KeyError(part)
    document = normal(document)
    current = get(document, part)
    if kind == "strategy" and not isinstance(current, str):
        raise KeyError(part)

    def check(data: Any) -> tuple[Any, list[ProtocolIssue]]:
        try:
            answer = ANSWER_MODELS[kind].model_validate(data)
        except ValidationError as e:
            return None, _pydantic_issues(e, ())
        if kind == "strategy":
            changes, issues = _strategy_changes(part, current, answer)
        else:
            changes, issues = _part_changes(part, kind, current, answer)
        if issues or not changes:
            return (None, issues) if issues else ((None, ()), [])
        suggested, issues = _compiled(apply(document, kind, part, changes))
        return (suggested, tuple(changes)), issues

    result, use, issues, raw = _converse(
        backend,
        recipe_id,
        purpose="improve",
        instructions=prompts.IMPROVE_INSTRUCTIONS,
        request=prompts.improve_request(kind, part, current, document),
        answer_model=ANSWER_MODELS[kind],
        check=check,
        max_tokens=IMPROVE_TOKENS,
    )
    suggested, changes = result if result else (None, ())
    return Answer(
        model=use,
        base_id=base_id,
        part=part,
        document=suggested,
        suggestion=Suggestion(part=part, kind=kind, changes=changes) if changes else None,
        issues=tuple(issues),
        raw_answer=raw,
    )


def _strategy_changes(
    part: str, strategy: str, answer: Any
) -> tuple[list[SuggestedChange], list[ProtocolIssue]]:
    changes: list[SuggestedChange] = []
    issues: list[ProtocolIssue] = []
    taken: list[tuple[int, int]] = []
    for i, edit in enumerate(answer.changes):
        at = path_of("changes", i)
        if not edit.find or edit.find == edit.replace:
            continue
        found = strategy.count(edit.find)
        if found != 1:
            message = (
                "is not in the current strategy; quote it exactly"
                if found == 0
                else f"occurs {found} times in the current strategy; quote enough to be unique"
            )
            issues.append(_issue(path_of(at, "find"), f"{edit.find!r} {message}"))
            continue
        start = strategy.index(edit.find)
        end = start + len(edit.find)
        if any(start < e and s < end for s, e in taken):
            issues.append(_issue(path_of(at, "find"), "overlaps another change"))
            continue
        alone = strategy[:start] + edit.replace + strategy[end:]
        for problem in strategy_problems(alone, None):
            issues.append(
                _issue(path_of(at, "replace"), f"on its own, this change leaves {problem}")
            )
        taken.append((start, end))
        changes.append(
            SuggestedChange(
                id="",
                path=part,
                field="strategy",
                before=edit.find,
                after=edit.replace,
                rationale=edit.rationale.strip(),
                start=start,
            )
        )
    changes.sort(key=lambda c: c.start or 0)
    return [c.model_copy(update={"id": f"c{n}"}) for n, c in enumerate(changes, 1)], issues


def _part_changes(
    part: str, kind: PartKind, current: dict[str, Any], answer: Any
) -> tuple[list[SuggestedChange], list[ProtocolIssue]]:
    model = PART_MODELS[kind]
    data = _with_extensions(current, answer.replacement.model_dump(mode="json"))
    data |= {k: current[k] for k in _FIXED_FIELDS if k in current}
    try:
        replacement = model.model_validate(data).model_dump(mode="json")
    except ValidationError as e:
        return [], _pydantic_issues(e, ("replacement",))
    notes: dict[str, list[str]] = {}
    for note in answer.changes:
        notes.setdefault(note.field.split(".")[0].split("[")[0], []).append(note.rationale.strip())
    changes = [
        SuggestedChange(
            id=f"c{n}",
            path=f"{part}.{field}",
            field=field,
            before=current.get(field),
            after=replacement[field],
            rationale=" ".join(notes.get(field, [])),
        )
        for n, field in enumerate(
            (
                f
                for f in model.model_fields
                if f not in _FIXED_FIELDS and replacement.get(f) != current.get(f)
            ),
            1,
        )
    ]
    return changes, []


def _with_extensions(current: Any, replacement: Any) -> Any:
    """`replacement` with the profile `extensions` of `current` at every level, objects in lists
    matched by ID. The model never sees a schema for them, so it can neither drop nor change
    them; an object it adds gets none."""
    if isinstance(current, dict) and isinstance(replacement, dict):
        out = {
            k: _with_extensions(current[k], v) if k in current else v
            for k, v in replacement.items()
        }
        if "extensions" in current:
            out["extensions"] = current["extensions"]
        return out
    if isinstance(current, list) and isinstance(replacement, list):
        by_id = {c["id"]: c for c in current if isinstance(c, dict) and "id" in c}
        return [
            _with_extensions(by_id[r["id"]], r)
            if isinstance(r, dict) and r.get("id") in by_id
            else r
            for r in replacement
        ]
    return replacement


def apply(
    document: dict[str, Any],
    kind: PartKind,
    part: str,
    changes: tuple[SuggestedChange, ...] | list[SuggestedChange],
) -> dict[str, Any]:
    """`document` with `changes` to `part` applied, and no others."""
    current = get(document, part)
    if kind == "strategy":
        text = current
        for change in sorted(changes, key=lambda c: c.start or 0, reverse=True):
            start = change.start or 0
            text = text[:start] + change.after + text[start + len(change.before) :]
        return replace(document, part, text)
    return replace(document, part, {**current, **{c.field: c.after for c in changes}})
