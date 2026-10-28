"""What the full-text screen shows beside the page: protocol-term mentions per criterion.

Mentions come from the protocol, never from the AI, so a reviewer may see them before deciding
(DESIGN.md, "Reviewer workbench"). Each criterion's terms are its keywords and the content words
of its wording; a mention is every whole-word occurrence in the layer, in document order, as an
unsaved anchor. Adopting one saves it (`POST /api/layers/{id}/anchors` with its offsets).
"""

import re
from typing import TYPE_CHECKING

from kittum.core import Id, Record, Span
from kittum.documents import Anchor, make_anchor

if TYPE_CHECKING:
    from kittum.documents import Layer
    from kittum.protocol import Criterion

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


__all__ = ["MAX_MENTIONS", "AssignmentFullText", "CriterionMentions", "mentions", "terms"]
