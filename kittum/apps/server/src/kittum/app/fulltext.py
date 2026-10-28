"""What the full-text screen shows beside the page, and the reviewer's "wrong document".

Mentions come from the protocol, never from the AI, so a reviewer may see them before deciding
(DESIGN.md, "Reviewer workbench"). Each criterion's terms are its keywords and the content words
of its wording; a mention is every whole-word occurrence in the layer, in document order, as an
unsaved anchor. Adopting one saves it (`POST /api/layers/{id}/anchors` with its offsets). Once the
reviewer's own provisional decision revealed the AI's reading, its verified quote comes as an
unsaved anchor too, so the page can highlight it in place.

A reviewer who finds that the PDF shown is not the record's report marks it as the wrong document
(`mark_wrong_document`): the item leaves screening, the document is detached from the record, and
the record has no full text again. That is not an exclusion.
"""

import re
from datetime import datetime
from typing import TYPE_CHECKING

from kittum.app.lifecycle import LifecycleError, full_text
from kittum.core import Id, Record, Span, new_id
from kittum.documents import Anchor, make_anchor
from kittum.ledger import EventDraft

if TYPE_CHECKING:
    from kittum.app.context import AppContext
    from kittum.db import Tx
    from kittum.documents import Layer
    from kittum.protocol import Criterion
    from kittum.screening import Reveal

FULL_TEXT_DETACHED = "project.full_text_detached"

MAX_MENTIONS = 40
"""Per criterion; a term on every page helps nobody."""

# Words of criterion wordings that say nothing about a particular record.
_COMMON = frozenset(
    [
        "about",
        "above",
        "after",
        "again",
        "against",
        "among",
        "another",
        "arm",
        "arms",
        "before",
        "being",
        "below",
        "between",
        "both",
        "compares",
        "control",
        "controls",
        "during",
        "either",
        "every",
        "first",
        "following",
        "follow",
        "form",
        "forms",
        "groups",
        "including",
        "least",
        "other",
        "others",
        "receives",
        "received",
        "report",
        "reports",
        "should",
        "since",
        "single",
        "start",
        "status",
        "their",
        "there",
        "these",
        "those",
        "three",
        "through",
        "treatment",
        "under",
        "until",
        "which",
        "while",
        "within",
        "without",
        "years",
        "older",
    ]
)


class CriterionMentions(Record):
    criterion_id: str
    terms: tuple[str, ...]
    mentions: tuple[Anchor, ...]


class AssignmentFullText(Record):
    """The full text behind a reviewer's full-text assignment."""

    document_id: Id
    layer_id: Id
    page_count: int | None
    licence: str | None
    mentions: tuple[CriterionMentions, ...]
    ai_quote: Anchor | None = None
    """The verified quote of the AI's reading shown after the reviewer's own provisional
    decision, as an unsaved anchor; None before that, and when the quote is not in the text."""


class WrongDocument(Record):
    recorded_at: datetime


def terms(criterion: Criterion, language: str = "en") -> tuple[str, ...]:
    """Keywords, then content words of the wording (five letters or more), lower-cased."""
    text = criterion.text.get(language) or next(iter(criterion.text.values()))
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z-]{4,}", text)]
    found = [k.lower() for k in criterion.keywords] + [w for w in words if w not in _COMMON]
    return tuple(dict.fromkeys(found))


def mentions(layer: Layer, criteria: tuple[Criterion, ...]) -> tuple[CriterionMentions, ...]:
    out = []
    for criterion in criteria:
        found = terms(criterion)
        spans: dict[int, Span] = {}
        for term in found:
            # A term matches as a word or a word's beginning ("smoke" finds "smokers").
            pattern = re.compile(rf"(?<![\w-]){re.escape(term.rstrip('s'))}[\w-]*", re.IGNORECASE)
            for m in pattern.finditer(layer.text):
                spans.setdefault(m.start(), Span(m.start(), m.end()))
        ordered = [spans[k] for k in sorted(spans)][:MAX_MENTIONS]
        out.append(
            CriterionMentions(
                criterion_id=criterion.id,
                terms=found,
                mentions=tuple(make_anchor(layer, s) for s in ordered),
            )
        )
    return tuple(out)


def ai_quote(layer: Layer, reveal: Reveal | None) -> Anchor | None:
    """The revealed quote on the page. A full-text item's text is its layer's text, so the
    reveal's text offsets are layer offsets."""
    span = reveal.quote_span if reveal else None
    if span is None or span.part != "text" or span.end > len(layer.text):
        return None
    return make_anchor(layer, Span(span.start, span.end))


def mark_wrong_document(
    tx: Tx,
    ctx: AppContext,
    project_id: Id,
    *,
    command_id: Id,
    reviewer_id: Id,
    assignment_id: Id,
    expected_etag: str,
    document_id: Id,
    note: str | None = None,
) -> WrongDocument:
    """The PDF of one of the reviewer's full-text assignments is not the record's report.

    The record leaves every full-text stage (`Screening.withdraw_item`), the document is detached from the
    record, and the ledger records who said so, when, and the note. The record then has no full
    text again: the retrieval list shows it as missing and PRISMA counts it as not retrieved. It is
    not an exclusion. `document_id` is the PDF the reviewer saw; if the record's full text is
    another one by now, nothing changes. A retry with the same `command_id` returns the first
    result.
    """
    screening = ctx.screening()
    ledger = ctx.require_ledger()
    # One operation under the project's command lock: a retry that arrives while the first
    # request is still running waits here, then finds its detachment and returns it.
    screening.lock_project(tx, project_id)
    done = tx.execute(
        "select recorded_at from full_text_detachment where command_id = %s", (command_id,)
    ).fetchone()
    if done is not None:
        return WrongDocument(recorded_at=done["recorded_at"])
    ref, info, _ = screening.assignment_source(tx, project_id, reviewer_id, assignment_id)
    found = full_text(tx, project_id, ref)
    if found is None or found.document_id != document_id:
        raise LifecycleError("This PDF is no longer the record's full text. Reload the queue.")
    screening.withdraw_item(
        tx,
        command_id=command_id,
        project_id=project_id,
        reviewer_id=reviewer_id,
        assignment_id=assignment_id,
        expected_etag=expected_etag,
        reason="wrong_document",
    )
    note = (note or "").strip() or None
    event = ledger.append(
        tx,
        EventDraft(
            project_id=project_id,
            type=FULL_TEXT_DETACHED,
            schema_version=1,
            actor_id=reviewer_id,
            payload={
                "item_ref": ref,
                "document_id": str(found.document_id),
                "layer_id": str(found.layer_id),
                "stage_id": str(info.stage_id),
                "reason": "wrong_document",
                "note": note,
            },
        ),
    )
    tx.execute("delete from full_text where project_id = %s and item_ref = %s", (project_id, ref))
    tx.execute(
        "insert into full_text_detachment (id, project_id, item_ref, document_id, reason, note,"
        " actor_id, command_id, event_id, recorded_at)"
        " values (%s, %s, %s, %s, 'wrong_document', %s, %s, %s, %s, %s)",
        (
            new_id(),
            project_id,
            ref,
            found.document_id,
            note,
            reviewer_id,
            command_id,
            event.event_id,
            event.recorded_at,
        ),
    )
    return WrongDocument(recorded_at=event.recorded_at)


__all__ = [
    "FULL_TEXT_DETACHED",
    "MAX_MENTIONS",
    "AssignmentFullText",
    "CriterionMentions",
    "WrongDocument",
    "ai_quote",
    "mark_wrong_document",
    "mentions",
    "terms",
]
