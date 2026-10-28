"""Assistant revisions in Postgres, the marks on what a model wrote, and publishing a revision.

Every revision compiles. A new revision carries its parent's AI marks for every field it leaves
as it was; a field it changes loses its mark unless the model wrote the new value.
"""

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
from kittum.assist.paths import changed_written, leaves, written
from kittum.core import Id, NotFoundError, new_id
from kittum.ledger import EventDraft
from kittum.protocol import (
    ProtocolDiff,
    ProtocolError,
    ProtocolVersion,
    StoredProtocol,
    compile,
    diff,
    publish,
    save_draft,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kittum.db import Tx
    from kittum.ledger import Ledger

ASSIST_EVENT = "protocol.assist"

_COLUMNS = (
    "id, project_id, seq, parent_id, kind, document, content_hash, actor_id, model, suggestion,"
    " accepted, ai_parts, note, created_at"
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
        ai_parts=tuple(AiMark.model_validate(m) for m in row["ai_parts"].values()),
        note=row["note"],
        created_at=row["created_at"],
    )


def list_revisions(tx: Tx, project_id: Id) -> list[ProtocolRevision]:
    rows = tx.execute(
        f"select {_COLUMNS} from assist_revision where project_id = %s order by seq",
        (project_id,),
    ).fetchall()
    return [_from_row(r) for r in rows]


def get_revision(tx: Tx, project_id: Id, revision_id: Id) -> ProtocolRevision:
    row = tx.execute(
        f"select {_COLUMNS} from assist_revision where id = %s and project_id = %s",
        (revision_id, project_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"assistant revision {revision_id} not found in project {project_id}")
    return _from_row(row)


def _carried(parent: ProtocolRevision | None, document: dict[str, Any]) -> dict[str, AiMark]:
    """The parent's marks on fields this document leaves unchanged."""
    if parent is None:
        return {}
    now, before = leaves(document), leaves(parent.document)
    return {
        m.path: m
        for m in parent.ai_parts
        if m.path in now and m.path in before and now[m.path] == before[m.path]
    }


def _mark(path: str, revision_id: Id, model: ModelUse, accepted_in: Id | None) -> AiMark:
    return AiMark(
        path=path,
        revision_id=revision_id,
        recipe_id=model.recipe_id,
        model_identity=model.model_identity,
        serving=model.serving,
        endpoint=model.endpoint,
        accepted_in=accepted_in,
    )


def _insert(
    tx: Tx,
    project_id: Id,
    actor_id: Id,
    *,
    kind: RevisionKind,
    version: ProtocolVersion,
    parent: ProtocolRevision | None,
    marks: Mapping[str, AiMark],
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
        f"insert into assist_revision (id, project_id, seq, parent_id, kind, document,"
        f" content_hash, actor_id, model, suggestion, accepted, ai_parts, note)"
        f" values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning {_COLUMNS}",
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
            Jsonb({p: m.model_dump(mode="json") for p, m in sorted(marks.items())}),
            note,
        ),
    ).fetchone()
    assert row is not None
    return _from_row(row)


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
        version = _compile(answer.document)
        if base is None:  # a draft: the model wrote every field
            parent = _latest(tx, project_id)
            marks = {p: _mark(p, revision_id, model, None) for p in written(answer.document)}
        else:
            parent = base
            marks = _carried(base, answer.document) | {
                p: _mark(p, revision_id, model, None)
                for p in changed_written(base.document, answer.document)
            }
        revision = _insert(
            tx,
            project_id,
            actor_id,
            kind="draft" if base is None else "suggestion",
            version=version,
            parent=parent,
            marks=marks,
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
    """The suggestion's base with only the chosen changes applied, as a new revision. The fields
    they change carry the model's mark, accepted in this revision. Raises `AssistError` for an
    unknown change or a choice that does not compile."""
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
    version = _compile(document)
    revision_id = new_id()
    marks = _carried(base, document) | {
        p: _mark(p, suggested.id, suggested.model, revision_id)
        for p in changed_written(base.document, document)
    }
    return _insert(
        tx,
        project_id,
        actor_id,
        kind="accepted",
        version=version,
        parent=base,
        marks=marks,
        revision_id=revision_id,
        accepted=Accepted(suggestion_id=suggested.id, change_ids=tuple(c.id for c in chosen)),
    )


def restore(tx: Tx, project_id: Id, actor_id: Id, revision_id: Id) -> ProtocolRevision:
    """An earlier revision, with its marks, as the newest one."""
    earlier = get_revision(tx, project_id, revision_id)
    return _insert(
        tx,
        project_id,
        actor_id,
        kind="restore",
        version=_compile(earlier.document),
        parent=earlier,
        marks={m.path: m for m in earlier.ai_parts},
        note=f"restores revision {earlier.seq}",
    )


def save_edit(
    tx: Tx, project_id: Id, actor_id: Id, source: dict[str, Any], *, base_id: Id | None = None
) -> ProtocolRevision:
    """The lead's own text as a revision. Edited from `base_id`, it keeps the base's AI marks on
    the fields the lead left as they were; a text of the lead's own (no base) carries none, even
    where it happens to say what a model once wrote."""
    base = get_revision(tx, project_id, base_id) if base_id else None
    version = _compile(source)
    document = version.protocol.model_dump(mode="json")
    return _insert(
        tx,
        project_id,
        actor_id,
        kind="edit",
        version=version,
        parent=base or _latest(tx, project_id),
        marks=_carried(base, document),
    )


def compare(tx: Tx, project_id: Id, a: Id, b: Id) -> ProtocolDiff:
    before, after = get_revision(tx, project_id, a), get_revision(tx, project_id, b)
    return diff(_compile(before.document), _compile(after.document))


def ai_parts(revision: ProtocolRevision) -> list[dict[str, Any]]:
    """The marks as a `protocol.published` event records them. Publishing accepts every part."""
    return [
        m.model_dump(mode="json") | {"accepted_in": str(m.accepted_in or revision.id)}
        for m in revision.ai_parts
    ]


def ai_parts_for(tx: Tx, project_id: Id, version: ProtocolVersion) -> list[dict[str, Any]]:
    """What a model wrote in a version published from the editor: the marks of the newest
    revision with the same content, or else the newest revision's marks on fields the version
    still holds as the model wrote them. Over-reporting a model's part is the safe error."""
    row = tx.execute(
        f"select {_COLUMNS} from assist_revision where project_id = %s"
        " order by (content_hash = %s) desc, seq desc limit 1",
        (project_id, version.content_hash),
    ).fetchone()
    if row is None:
        return []
    newest = _from_row(row)
    document = version.protocol.model_dump(mode="json")
    kept = _carried(newest, document)
    return ai_parts(newest.model_copy(update={"ai_parts": tuple(kept.values())}))


def publish_revision(
    tx: Tx,
    project_id: Id,
    revision_id: Id,
    approved_by: Id,
    *,
    ledger: Ledger,
    reason: str | None = None,
) -> StoredProtocol:
    """Publish a revision as the protocol in force, with the approver's approval and the parts a
    model wrote in the `protocol.published` event."""
    revision = get_revision(tx, project_id, revision_id)
    draft = save_draft(tx, project_id, _compile(revision.document))
    return publish(
        tx,
        project_id,
        draft.id,
        approved_by,
        ledger=ledger,
        reason=reason,
        ai_parts=ai_parts(revision),
    )
