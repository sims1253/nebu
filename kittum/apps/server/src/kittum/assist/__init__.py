"""The protocol assistant: a model drafts a protocol from the lead's description, and improves
one part of it at a time (a review question, a criterion, a source's search strategy).

    answer = draft(backend, recipe_id, "Adults who smoke ...")      # model calls only
    outcome = record(tx, project_id, lead_id, answer, ledger=ledger) # a revision, or the problems
    answer = improve(backend, recipe_id, revision.id, revision.document, part)
    accepted = accept(tx, project_id, lead_id, suggestion_id, ["c1", "c3"])
    publish_revision(tx, project_id, accepted.id, lead_id, ledger=ledger)

Every answer is in a JSON Schema derived from the protocol's own models (`strict_schema`) and goes
through `kittum.protocol.compile` like a hand-written source; an answer with problems goes back
to the model with them, at most twice. Nothing the model writes is published by it: each draft,
suggestion, accepted selection, edit, and restore is an append-only revision, and only a lead
publishes one. The fields a model wrote carry marks (which field, which revision, which model)
into the `protocol.published` event and from there into the AI disclosure. Every request is a
`protocol.assist` ledger event with each attempt's raw request and response by hash.

Importing this package registers `ai_parts_for` with `kittum.protocol.register_attribution`, so
every publication names the fields a model wrote.
"""

from kittum.assist.answers import REPAIRS, StrategyAnswer, apply, draft, improve, part_kind
from kittum.assist.model import (
    Accepted,
    AiMark,
    Answer,
    AssistError,
    AssistOutcome,
    ModelAttempt,
    ModelUse,
    PartKind,
    ProtocolRevision,
    RevisionKind,
    SuggestedChange,
    Suggestion,
)
from kittum.assist.paths import get as value_at
from kittum.assist.prompts import PROMPT_VERSION
from kittum.assist.schema import from_strict, strict_schema
from kittum.assist.store import (
    ASSIST_EVENT,
    accept,
    ai_parts_for,
    compare,
    get_revision,
    list_revisions,
    publish_revision,
    record,
    restore,
    save_edit,
)
from kittum.protocol import register_attribution

__all__ = [
    "ASSIST_EVENT",
    "PROMPT_VERSION",
    "REPAIRS",
    "Accepted",
    "AiMark",
    "Answer",
    "AssistError",
    "AssistOutcome",
    "ModelAttempt",
    "ModelUse",
    "PartKind",
    "ProtocolRevision",
    "RevisionKind",
    "StrategyAnswer",
    "SuggestedChange",
    "Suggestion",
    "accept",
    "ai_parts_for",
    "apply",
    "compare",
    "draft",
    "from_strict",
    "get_revision",
    "improve",
    "list_revisions",
    "part_kind",
    "publish_revision",
    "record",
    "restore",
    "save_edit",
    "strict_schema",
    "value_at",
]

# Every publication, from whichever entry point, names the fields a model wrote.
register_attribution("assist", ai_parts_for)
