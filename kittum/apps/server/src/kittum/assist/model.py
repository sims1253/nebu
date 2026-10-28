"""The values of the protocol assistant."""

from datetime import datetime
from typing import Any, Literal

from kittum.core import Id, KittumError, Record, Sha256
from kittum.protocol import ProtocolIssue

type PartKind = Literal["question", "criterion", "strategy"]
type RevisionKind = Literal["draft", "suggestion", "accepted", "edit", "restore"]
type Purpose = Literal["draft", "improve"]


class AssistError(KittumError):
    """A request the assistant cannot carry out as asked: an unknown part, a change that is not in
    the suggestion, or a selection of changes that does not compile (`issues` names why)."""

    def __init__(self, message: str, issues: tuple[ProtocolIssue, ...] = ()) -> None:
        super().__init__(message)
        self.issues = issues


class ModelAttempt(Record):
    """One model call. A rejected answer carries the problems Kittum sent back to the model."""

    request_hash: Sha256
    raw_request_hash: Sha256 | None
    raw_response_hash: Sha256 | None
    model_identity: str | None
    finish_reason: str | None = None
    issues: tuple[ProtocolIssue, ...] = ()


class ModelUse(Record):
    """Who wrote a model revision, and how: the recipe, where it ran, the fixed instructions by
    version and hash, and every attempt with its raw request and response in the artifact store."""

    purpose: Purpose
    recipe_id: str
    model_identity: str | None
    serving: str | None
    endpoint: str | None
    prompt_version: str
    prompt_hash: Sha256
    attempts: tuple[ModelAttempt, ...]
    event_id: Id | None = None


class AiMark(Record):
    """A field a model wrote: the model revision it came from, and the revision in which the
    lead accepted it (None while it is only suggested)."""

    path: str
    revision_id: Id
    recipe_id: str
    model_identity: str | None
    serving: str | None
    endpoint: str | None
    accepted_in: Id | None


class SuggestedChange(Record):
    """One change the model suggests, which the lead accepts or not on its own. For a strategy,
    `before` is the passage it replaces at `start` and `after` its replacement; otherwise both are
    the whole field."""

    id: str
    path: str
    field: str
    before: Any = None
    after: Any = None
    rationale: str
    start: int | None = None


class Suggestion(Record):
    part: str
    kind: PartKind
    changes: tuple[SuggestedChange, ...]


class Accepted(Record):
    suggestion_id: Id
    change_ids: tuple[str, ...]


class ProtocolRevision(Record):
    id: Id
    project_id: Id
    seq: int
    parent_id: Id | None
    kind: RevisionKind
    document: dict[str, Any]
    content_hash: Sha256
    actor_id: Id
    model: ModelUse | None = None
    suggestion: Suggestion | None = None
    accepted: Accepted | None = None
    ai_parts: tuple[AiMark, ...] = ()
    note: str | None = None
    created_at: datetime


class Answer(Record):
    """What a model produced for a request, checked, before anything is stored. `document` is the
    compiled draft, or the base with every suggested change applied."""

    model: ModelUse
    base_id: Id | None = None
    part: str | None = None
    document: dict[str, Any] | None = None
    suggestion: Suggestion | None = None
    issues: tuple[ProtocolIssue, ...] = ()
    raw_answer: str | None = None

    @property
    def ok(self) -> bool:
        return self.document is not None


class AssistOutcome(Record):
    """The result of asking the assistant. Without a revision, `issues` says why: the answer
    still did not compile after the repair rounds (with the last raw answer), or it changed
    nothing."""

    revision: ProtocolRevision | None
    issues: tuple[ProtocolIssue, ...] = ()
    raw_answer: str | None = None
    attempts: int
    unchanged: bool = False
    event_id: Id
