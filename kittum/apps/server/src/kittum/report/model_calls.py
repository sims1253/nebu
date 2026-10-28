"""The model calls behind the review's AI steps, and the prompts they sent.

Every model call stores the exact request body and the exact response in the artifact store,
by SHA-256 (`kittum.models`). Three places name them:

- the judge's verdict rows list each attempt (`verdict.attempts`), and a `judge.verdict_recorded`
  event names its verdict;
- a `protocol.assist` event lists the assistant's attempts in its payload;
- an extraction run's transcript, which `extraction.proposals_recorded` names, lists the LLM
  path's calls.

`calls_of` finds the calls behind one ledger event, for the audit trail. `prompt_cards` reads the
first request of each AI role back from the store, for the AI-use disclosure: the system prompt,
the user prompt, the JSON schema, and the request settings exactly as the model server got them.

A backend that wrote no request body (a recorded or replayed model) leaves `raw_request_hash`
empty. The judge then still has the request as Kittum built it (`rendered_request`), which holds
the same messages and schema without the server's own settings.
"""

import json
from typing import TYPE_CHECKING, Any, Literal

from kittum.core import Id, Record, Sha256

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import AuditEvent
    from kittum.storage import ArtifactStore

type Role = Literal["judge", "assistant", "extraction"]

VERDICT_RECORDED = "judge.verdict_recorded"
ASSIST_EVENT = "protocol.assist"
PROPOSALS_RECORDED = "extraction.proposals_recorded"

# Keys of a request body that are the prompt itself, not a setting.
_PROMPT_KEYS = {"messages", "response_format", "json_schema", "model", "model_id", "stream"}


class CallPart(Record):
    """One side of a model call in the artifact store."""

    sha256: Sha256
    form: Literal["http_body", "built_request", "http_response", "answer_text"]
    """`http_body` is the JSON sent to the model server; `built_request` the request as Kittum
    built it before a backend that stores no body; `http_response` the server's response as
    received; `answer_text` the answer alone, when the backend kept no response."""
    stored: bool


class ModelCall(Record):
    """One request to a model and its response."""

    number: int
    request: CallPart | None
    response: CallPart | None
    model_identity: str | None = None
    finish_reason: str | None = None
    problem: str | None = None
    """Why the call failed, or what Kittum sent back to the model about its answer."""


class SentRequest(Record):
    """A stored request, read back."""

    model: str | None
    messages: tuple[tuple[str, str], ...]  # (role, content)
    json_schema: dict[str, Any] | None
    settings: dict[str, Any]
    """Everything else the body set: temperature, max_tokens, seed, reasoning_effort, and the
    recipe's other server options."""


def read_request(data: bytes) -> SentRequest | None:
    """A request body (`http_body`) or a built request (`built_request`), or None if it is
    neither."""
    try:
        body = json.loads(data)
    except ValueError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return None
    schema = body.get("json_schema")
    response_format = body.get("response_format")
    if isinstance(response_format, dict):
        schema = (response_format.get("json_schema") or {}).get("schema", schema)
    return SentRequest(
        model=body.get("model") or body.get("model_id"),
        messages=tuple(
            (str(m.get("role")), str(m.get("content")))
            for m in body["messages"]
            if isinstance(m, dict)
        ),
        json_schema=schema if isinstance(schema, dict) else None,
        settings={k: v for k, v in body.items() if k not in _PROMPT_KEYS and v is not None},
    )


def _part(store: ArtifactStore | None, sha256: Any, form: Any) -> CallPart | None:
    if not sha256:
        return None
    return CallPart(sha256=str(sha256), form=form, stored=bool(store and store.exists(str(sha256))))


def judge_calls(attempts: list[dict[str, Any]], store: ArtifactStore | None) -> list[ModelCall]:
    calls = []
    for n, a in enumerate(attempts, start=1):
        request = _part(store, a.get("raw_request_hash"), "http_body")
        if request is None or not request.stored:
            request = _part(store, a.get("rendered_request"), "built_request") or request
        response = _part(store, a.get("raw_response_hash"), "http_response")
        if response is None or not response.stored:
            response = _part(store, a.get("output_hash"), "answer_text") or response
        calls.append(
            ModelCall(
                number=n,
                request=request,
                response=response,
                model_identity=a.get("model_identity"),
                finish_reason=a.get("finish_reason"),
                problem=a.get("error"),
            )
        )
    return calls


def assist_calls(payload: dict[str, Any], store: ArtifactStore | None) -> list[ModelCall]:
    calls = []
    for n, a in enumerate(payload.get("attempts", []), start=1):
        issues = [
            f"{i.get('path') or 'the whole answer'}: {i.get('message')}"
            for i in a.get("issues", [])
        ]
        calls.append(
            ModelCall(
                number=n,
                request=_part(store, a.get("raw_request_hash"), "http_body"),
                response=_part(store, a.get("raw_response_hash"), "http_response"),
                model_identity=a.get("model_identity"),
                finish_reason=a.get("finish_reason"),
                problem="; ".join(issues) or None,
            )
        )
    return calls


def transcript(store: ArtifactStore | None, sha256: str | None) -> dict[str, Any] | None:
    """An extraction run's transcript, if the store holds it."""
    if not sha256 or store is None or not store.exists(sha256):
        return None
    try:
        found = json.loads(store.get(sha256))
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def extraction_calls(found: dict[str, Any] | None, store: ArtifactStore | None) -> list[ModelCall]:
    if found is None:
        return []
    return [
        ModelCall(
            number=n,
            request=_part(store, c.get("raw_request_hash"), "http_body"),
            response=_part(store, c.get("raw_response_hash"), "http_response"),
            model_identity=found.get("model_identity"),
            finish_reason=c.get("finish_reason"),
        )
        for n, c in enumerate(found.get("calls", []), start=1)
    ]


def calls_of(tx: Tx, e: AuditEvent, store: ArtifactStore | None) -> list[ModelCall]:
    """The model calls behind one ledger event; none for an event that is not an AI step."""
    p = e.payload
    if e.type == VERDICT_RECORDED and p.get("verdict_id"):
        row = tx.execute(
            "select attempts from verdict where id = %s", (p["verdict_id"],)
        ).fetchone()
        return judge_calls(row["attempts"], store) if row else []
    if e.type == ASSIST_EVENT:
        return assist_calls(p, store)
    if e.type == PROPOSALS_RECORDED:
        return extraction_calls(transcript(store, p.get("transcript_hash")), store)
    return []


def named_by_calls(tx: Tx, project_id: Id, sha256: str, store: ArtifactStore | None) -> bool:
    """Whether a model call of the project names the artifact: a judge attempt's request or
    response, or an extraction call's (the transcript holds those, not the ledger)."""
    row = tx.execute(
        "select 1 from verdict where project_id = %s and strpos(attempts::text, %s) > 0 limit 1",
        (project_id, f'"{sha256}"'),
    ).fetchone()
    if row is not None:
        return True
    for run in tx.execute(
        "select transcript_hash from extraction_run where project_id = %s"
        " and transcript_hash is not null",
        (project_id,),
    ).fetchall():
        found = transcript(store, run["transcript_hash"].strip())
        for c in (found or {}).get("calls", []):
            if sha256 in (c.get("raw_request_hash"), c.get("raw_response_hash")):
                return True
    return False


# The prompts each AI role sent, for the disclosure.


class PromptCard(Record):
    """What one AI role sent the model, read from its first stored request."""

    role: Role
    label: str
    """Which use of the role: "Title and abstract, scope X" for the judge, "Draft" or "Improve"
    for the assistant."""
    prompt_version: str | None
    prompt_hash: str | None
    recipe_id: str | None
    model: dict[str, Any]
    """The recipe as recorded: repository, revision, weights, licence, serving, and where the
    calls went. The assistant and the extraction path record less than the judge."""
    request_settings: dict[str, Any]
    system_prompt: str | None
    user_prompt_template: str | None
    """The template the role fills in per call, where the role has one."""
    first_user_prompt: str | None
    """The user prompt of the first call, exactly as sent."""
    json_schema: dict[str, Any] | None
    request_sha256: Sha256 | None
    """The stored request these were read from."""
    request_form: str | None
    calls: int


def _card(
    role: Role,
    label: str,
    calls: list[ModelCall],
    store: ArtifactStore | None,
    *,
    total: int,
    prompt_version: str | None,
    prompt_hash: str | None,
    recipe_id: str | None,
    model: dict[str, Any],
    user_template: str | None = None,
) -> PromptCard:
    sent: SentRequest | None = None
    source: CallPart | None = None
    for call in calls:
        if call.request is not None and call.request.stored and store is not None:
            sent = read_request(store.get(call.request.sha256))
            if sent is not None:
                source = call.request
                break
    system = next((c for r, c in sent.messages if r == "system"), None) if sent else None
    user = next((c for r, c in sent.messages if r == "user"), None) if sent else None
    if sent and sent.model and "model_as_sent" not in model:
        model = {**model, "model_as_sent": sent.model}
    return PromptCard(
        role=role,
        label=label,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        recipe_id=recipe_id,
        model=model,
        request_settings=sent.settings if sent else {},
        system_prompt=system,
        user_prompt_template=user_template,
        first_user_prompt=user,
        json_schema=sent.json_schema if sent else None,
        request_sha256=source.sha256 if source else None,
        request_form=source.form if source else None,
        calls=total,
    )


STAGE_LABELS = {"title_abstract": "Title and abstract", "full_text": "Full text"}


def prompt_cards(
    tx: Tx,
    project_id: Id,
    events: list[AuditEvent],
    judges: dict[str, dict[str, Any]],
    store: ArtifactStore | None,
    *,
    judge_template: str | None = None,
) -> tuple[PromptCard, ...]:
    """One card per use of each AI role through the cutoff, in the order the review first used
    them. `judges` holds each judge configuration's recorded recipe and prompt hashes by ID."""
    cards: list[PromptCard] = []

    verdict_ids = [e.payload["verdict_id"] for e in events if e.type == VERDICT_RECORDED]
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in tx.execute(
        "select judge_config_id, stage, scope_id, attempts, created_at from verdict"
        " where project_id = %s and id = any(%s::uuid[]) order by created_at, id",
        (project_id, verdict_ids),
    ).fetchall():
        groups.setdefault((row["judge_config_id"], row["stage"], row["scope_id"]), []).append(row)
    for (config_id, stage, scope), rows in groups.items():
        judge = judges.get(config_id, {})
        recipe = judge.get("recipe") or {}
        cards.append(
            _card(
                "judge",
                f"{STAGE_LABELS.get(stage, stage)}, scope {scope}",
                [c for r in rows[:3] for c in judge_calls(r["attempts"], store)],
                store,
                total=sum(len(r["attempts"]) for r in rows),
                prompt_version=judge.get("prompt_version"),
                prompt_hash=judge.get("prompt_hash"),
                recipe_id=judge.get("model_recipe_id"),
                model={
                    "repository": recipe.get("repository"),
                    "revision": recipe.get("revision"),
                    "weights_hash": judge.get("weights_hash"),
                    "weights_licence": recipe.get("weights_licence"),
                    "serving": recipe.get("serving"),
                    "extra_body": recipe.get("extra_body") or {},
                    "endpoints": list(judge.get("endpoints", [])),
                    "reported_model_identities": list(judge.get("model_identities", [])),
                },
                user_template=judge_template,
            )
        )

    assist: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.type == ASSIST_EVENT:
            assist.setdefault(str(e.payload.get("purpose")), []).append(e.payload)
    for purpose, payloads in assist.items():
        first = payloads[0]
        cards.append(
            _card(
                "assistant",
                {"draft": "Draft the protocol", "improve": "Improve one part"}.get(
                    purpose, purpose
                ),
                [c for p in payloads[:3] for c in assist_calls(p, store)],
                store,
                total=sum(len(p.get("attempts", [])) for p in payloads),
                prompt_version=first.get("prompt_version"),
                prompt_hash=first.get("prompt_hash"),
                recipe_id=first.get("recipe_id"),
                model={
                    "serving": first.get("serving"),
                    "endpoints": sorted({p["endpoint"] for p in payloads if p.get("endpoint")}),
                    "reported_model_identities": sorted(
                        {p["model_identity"] for p in payloads if p.get("model_identity")}
                    ),
                },
            )
        )

    cutoff = max((e.seq for e in events), default=0)
    runs = tx.execute(
        "select agent, model_identity, transcript_hash from extraction_run"
        " where project_id = %s and agent is not null and recorded_seq <= %s"
        " order by recorded_seq",
        (project_id, cutoff),
    ).fetchall()
    if runs:
        transcripts = [transcript(store, (r["transcript_hash"] or "").strip()) for r in runs]
        calls = [c for t in transcripts[:3] for c in extraction_calls(t, store)]
        agent = runs[0]["agent"] or {}
        first = next((t for t in transcripts if t), {}) or {}
        cards.append(
            _card(
                "extraction",
                "LLM extraction path",
                calls,
                store,
                total=sum(len((t or {}).get("calls", [])) for t in transcripts),
                prompt_version=first.get("prompt_version"),
                prompt_hash=None,
                recipe_id=agent.get("model_id"),
                model={
                    "serving": first.get("serving"),
                    "endpoints": sorted(
                        {t["endpoint"] for t in transcripts if t and t.get("endpoint")}
                    ),
                    "reported_model_identities": sorted(
                        {r["model_identity"] for r in runs if r["model_identity"]}
                    ),
                },
            )
        )
    return tuple(cards)
