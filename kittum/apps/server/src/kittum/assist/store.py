"""Assistant revisions in Postgres, the marks on what a model wrote, and publishing a revision.

Every revision compiles. What a model wrote is attributed by value, from the project's whole
assistant history (drafts, suggestions, accepted selections), however the document was saved
since: an accepted change, the lead's own text with or without a base, a restore, the plain
editor, the CLI. Free text is the model's when a model wrote those words for that kind of field
anywhere, whatever the object is now called or wherever it moved; choices, numbers, and booleans
only at the same path. A person who changes a value takes the field over; changing it back gives
it back to the model. Marks are computed when revisions are read, from the history up to each
one, and at every publication from all of it (`ai_parts_for`, registered with
`kittum.protocol.register_attribution` when `kittum.assist` is imported).
"""

import functools
import json
import re
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Json, Jsonb

from kittum.assist.answers import apply
from kittum.assist.model import (
    Accepted,
    AiMark,
    Answer,
    AssistError,
    AssistOutcome,
    ModelUse,
    ProtocolRevision,
    RevisionKind,
    Suggestion,
)
from kittum.assist.paths import changed_written, steps, written
from kittum.core import Id, NotFoundError, new_id
from kittum.ledger import EventDraft
from kittum.protocol import (
    ProtocolDiff,
    ProtocolError,
    ProtocolVersion,
    ReviewProtocol,
    StoredProtocol,
    compile,
    diff,
    publish,
    save_draft,
)

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.ledger import Ledger

ASSIST_EVENT = "protocol.assist"

_COLUMNS = (
    "id, project_id, seq, parent_id, kind, document, content_hash, actor_id, model, suggestion,"
    " accepted, note, created_at"
)


def _from_row(row: dict[str, Any]) -> ProtocolRevision:
    return ProtocolRevision(
        id=row["id"],
        project_id=row["project_id"],
        seq=row["seq"],
        parent_id=row["parent_id"],
        kind=row["kind"],
        document=row["document"],
        content_hash=row["content_hash"],
        actor_id=row["actor_id"],
        model=ModelUse.model_validate(row["model"]) if row["model"] else None,
        suggestion=Suggestion.model_validate(row["suggestion"]) if row["suggestion"] else None,
        accepted=Accepted.model_validate(row["accepted"]) if row["accepted"] else None,
        note=row["note"],
        created_at=row["created_at"],
    )


class _History:
    """What models wrote, by field and value, in revision order, and where a person first kept it.

    Free text (a title, a criterion's text, a population, a strategy, an outcome's name, a
    rationale) is matched by the kind of field, its path without the IDs of the objects on the
    way (`search_topics[*].criteria[*].text.en`): the words stay the model's when their criterion
    is renamed or moved, or their question reordered. Choices (IDs, a polarity, a mode, a kind, a
    source's name and platform), numbers, and booleans are matched at their exact path, so a
    lead's own protocol that happens to choose what a model chose elsewhere is not the model's.
    Free text errs on the side of naming the model, the safe side for a disclosure."""

    def __init__(self) -> None:
        self._written: dict[tuple[str, str], ProtocolRevision] = {}
        self._kept: dict[tuple[str, str], Id] = {}
        self._revisions: dict[Id, ProtocolRevision] = {}

    def add(self, revision: ProtocolRevision) -> None:
        self._revisions[revision.id] = revision
        values = written(revision.document)
        author = revision
        wrote: dict[str, Any] = {}
        if revision.kind == "draft":
            wrote = values
        elif revision.kind in ("suggestion", "accepted") and revision.parent_id is not None:
            base = self._revisions[revision.parent_id].document  # a base comes before
            wrote = {p: values[p] for p in changed_written(base, revision.document)}
            if revision.accepted is not None:
                author = self._revisions[revision.accepted.suggestion_id]
        for key in map(_key, wrote.items()):
            self._written.setdefault(key, author)
        if revision.model is None:  # a revision of the lead's keeps what it holds
            for key in map(_key, values.items()):
                if key in self._written:
                    self._kept.setdefault(key, revision.id)

    def marks(self, document: dict[str, Any]) -> tuple[AiMark, ...]:
        found = []
        for path, value in written(document).items():
            key = _key((path, value))
            source = self._written.get(key)
            if source is None or source.model is None:
                continue
            found.append(
                AiMark(
                    path=path,
                    revision_id=source.id,
                    recipe_id=source.model.recipe_id,
                    model_identity=source.model.model_identity,
                    serving=source.model.serving,
                    endpoint=source.model.endpoint,
                    accepted_in=self._kept.get(key),
                )
            )
        return tuple(found)


_IDS = re.compile(r"\[[^\]]*\]")
_SCHEMA = ReviewProtocol.model_json_schema(mode="validation")
# Free strings in the schema that name an entry of the source list rather than say anything.
_CHOSEN = frozenset(
    {
        "search_topics[*].source_plan.sources[*].name",
        "search_topics[*].source_plan.sources[*].platform",
    }
)


def _key(item: tuple[str, Any]) -> tuple[str, str]:
    """Where a value counts as the same field: its kind for free text, its path otherwise."""
    path, value = item
    kind = _IDS.sub("[*]", path)
    return kind if _free_text(kind) else path, json.dumps(value, sort_keys=True, ensure_ascii=False)


@functools.cache
def _free_text(kind: str) -> bool:
    """Whether the protocol's schema makes the field at `kind` free text: a string, or a list of
    strings, with no enumeration, constant, or pattern (an ID's). Unknown paths are not."""
    if kind in _CHOSEN or kind.rsplit(".", 1)[-1] == "id":
        return False
    defs = _SCHEMA.get("$defs", {})

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        while True:
            if "$ref" in node:
                node = defs[node["$ref"].rsplit("/", 1)[-1]]
            elif "anyOf" in node:
                node = next(b for b in node["anyOf"] if b != {"type": "null"})
            else:
                return node

    node = resolve(_SCHEMA)
    try:
        for step in steps(kind):
            if isinstance(step, tuple):
                node = resolve(node["items"])
            elif "properties" in node:
                node = resolve(node["properties"][step])
            else:  # a mapping, such as a text per language
                node = resolve(node["additionalProperties"])
    except KeyError, TypeError, StopIteration:
        return False
    if node.get("type") == "array" and isinstance(node.get("items"), dict):
        node = resolve(node["items"])
    return node.get("type") == "string" and not {"enum", "const", "pattern"} & set(node)


def _history(tx: Tx, project_id: Id) -> tuple[_History, list[ProtocolRevision]]:
    rows = tx.execute(
        f"select {_COLUMNS} from assist_revision where project_id = %s order by seq",
        (project_id,),
    ).fetchall()
    history = _History()
    revisions = []
    for row in rows:
        revision = _from_row(row)
        history.add(revision)
        revisions.append(revision.model_copy(update={"ai_parts": history.marks(revision.document)}))
    return history, revisions


def list_revisions(tx: Tx, project_id: Id) -> list[ProtocolRevision]:
    """Every revision, oldest first, each with the marks the history up to it gives."""
    return _history(tx, project_id)[1]


def get_revision(tx: Tx, project_id: Id, revision_id: Id) -> ProtocolRevision:
    found = next((r for r in list_revisions(tx, project_id) if r.id == revision_id), None)
    if found is None:
        raise NotFoundError(f"assistant revision {revision_id} not found in project {project_id}")
    return found


def _insert(
    tx: Tx,
    project_id: Id,
    actor_id: Id,
    *,
    kind: RevisionKind,
    version: ProtocolVersion,
    parent: ProtocolRevision | None,
    revision_id: Id | None = None,
    model: ModelUse | None = None,
    suggestion: Suggestion | None = None,
    accepted: Accepted | None = None,
    note: str | None = None,
) -> ProtocolRevision:
    tx.execute(
        "select pg_advisory_xact_lock(hashtext('kittum.assist'), hashtext(%s))", (str(project_id),)
    )
    seq = tx.execute(
        "select coalesce(max(seq), 0) + 1 as seq from assist_revision where project_id = %s",
        (project_id,),
    ).fetchone()
    assert seq is not None

    def dump(value: Any) -> Jsonb | None:
        return Jsonb(value.model_dump(mode="json")) if value is not None else None

    row = tx.execute(
        "insert into assist_revision (id, project_id, seq, parent_id, kind, document,"
        " content_hash, actor_id, model, suggestion, accepted, note)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (
            revision_id or new_id(),
            project_id,
            seq["seq"],
            parent.id if parent else None,
            kind,
            Json(version.protocol.model_dump(mode="json")),
            version.content_hash,
            actor_id,
            dump(model),
            dump(suggestion),
            dump(accepted),
            note,
        ),
    ).fetchone()
    assert row is not None
    return get_revision(tx, project_id, row["id"])


def _compile(document: dict[str, Any]) -> ProtocolVersion:
    try:
        return compile(document)
    except ProtocolError as e:
        raise AssistError("the protocol does not compile", e.issues) from None


def record(
    tx: Tx, project_id: Id, actor_id: Id, answer: Answer, *, ledger: Ledger
) -> AssistOutcome:
    """Store what the model answered for the lead: a draft or suggestion revision when the answer
    compiled, nothing otherwise. Either way the ledger records the request and every attempt with
    its raw artifacts, for the disclosure."""
    base = get_revision(tx, project_id, answer.base_id) if answer.base_id else None
    revision_id = new_id() if answer.ok else None
    outcome = "revision" if answer.ok else ("unchanged" if not answer.issues else "failed")
    model = answer.model
    event = ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type=ASSIST_EVENT,
            schema_version=1,
            actor_id=actor_id,
            payload={
                "purpose": model.purpose,
                "outcome": outcome,
                "revision_id": str(revision_id) if revision_id else None,
                "base_revision_id": str(base.id) if base else None,
                "part": answer.part,
                "recipe_id": model.recipe_id,
                "model_identity": model.model_identity,
                "serving": model.serving,
                "endpoint": model.endpoint,
                "prompt_version": model.prompt_version,
                "prompt_hash": model.prompt_hash,
                "attempts": [a.model_dump(mode="json") for a in model.attempts],
            },
        ),
    )
    model = model.model_copy(update={"event_id": event.event_id})
    revision = None
    if answer.document is not None and revision_id is not None:
        revision = _insert(
            tx,
            project_id,
            actor_id,
            kind="draft" if base is None else "suggestion",
            version=_compile(answer.document),
            parent=base or _latest(tx, project_id),
            revision_id=revision_id,
            model=model,
            suggestion=answer.suggestion,
        )
    return AssistOutcome(
        revision=revision,
        issues=answer.issues,
        raw_answer=None if answer.ok else answer.raw_answer,
        attempts=len(model.attempts),
        unchanged=outcome == "unchanged",
        event_id=event.event_id,
    )


def _latest(tx: Tx, project_id: Id) -> ProtocolRevision | None:
    revisions = list_revisions(tx, project_id)
    return revisions[-1] if revisions else None


def accept(
    tx: Tx, project_id: Id, actor_id: Id, suggestion_id: Id, change_ids: list[str]
) -> ProtocolRevision:
    """The suggestion's base with only the chosen changes applied, as a new revision. Raises
    `AssistError` for an unknown change or a choice that does not compile."""
    suggested = get_revision(tx, project_id, suggestion_id)
    if suggested.suggestion is None or suggested.parent_id is None or suggested.model is None:
        raise AssistError(f"revision {suggestion_id} is not a suggestion")
    known = {c.id: c for c in suggested.suggestion.changes}
    unknown = [c for c in change_ids if c not in known]
    if unknown or not change_ids:
        raise AssistError(
            f"the suggestion has no change {', '.join(unknown)}" if unknown else "choose a change"
        )
    base = get_revision(tx, project_id, suggested.parent_id)
    chosen = [known[c] for c in sorted(set(change_ids), key=list(known).index)]
    document = apply(base.document, suggested.suggestion.kind, suggested.suggestion.part, chosen)
    return _insert(
        tx,
        project_id,
        actor_id,
        kind="accepted",
        version=_compile(document),
        parent=base,
        accepted=Accepted(suggestion_id=suggested.id, change_ids=tuple(c.id for c in chosen)),
    )


def restore(tx: Tx, project_id: Id, actor_id: Id, revision_id: Id) -> ProtocolRevision:
    """An earlier revision as the newest one."""
    earlier = get_revision(tx, project_id, revision_id)
    return _insert(
        tx,
        project_id,
        actor_id,
        kind="restore",
        version=_compile(earlier.document),
        parent=earlier,
        note=f"restores revision {earlier.seq}",
    )


def save_edit(
    tx: Tx, project_id: Id, actor_id: Id, source: dict[str, Any], *, base_id: Id | None = None
) -> ProtocolRevision:
    """The lead's own text as a revision, edited from `base_id` or written anew. Whatever in it
    still says what a model wrote stays the model's."""
    base = get_revision(tx, project_id, base_id) if base_id else None
    return _insert(
        tx,
        project_id,
        actor_id,
        kind="edit",
        version=_compile(source),
        parent=base or _latest(tx, project_id),
    )


def compare(tx: Tx, project_id: Id, a: Id, b: Id) -> ProtocolDiff:
    before, after = get_revision(tx, project_id, a), get_revision(tx, project_id, b)
    return diff(_compile(before.document), _compile(after.document))


def ai_parts_for(tx: Tx, project_id: Id, version: ProtocolVersion) -> list[dict[str, Any]]:
    """The fields of `version` a model wrote, from the project's whole assistant history, as
    `protocol.published` records them. The `kittum.protocol.Attribution` of the assistant."""
    history, _ = _history(tx, project_id)
    return [
        m.model_dump(mode="json") for m in history.marks(version.protocol.model_dump(mode="json"))
    ]


def publish_revision(
    tx: Tx,
    project_id: Id,
    revision_id: Id,
    approved_by: Id,
    *,
    ledger: Ledger,
    reason: str | None = None,
) -> StoredProtocol:
    """Publish a revision as the protocol in force, with the approver's approval. The publication
    names the fields a model wrote, as every publication does."""
    revision = get_revision(tx, project_id, revision_id)
    draft = save_draft(tx, project_id, _compile(revision.document))
    return publish(tx, project_id, draft.id, approved_by, ledger=ledger, reason=reason)
