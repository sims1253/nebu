"""Anchors into a layer, and re-anchoring onto a new layer of the same PDF.

`make_anchor` and `resolve` do no I/O; `documents.remap` stores what `resolve` decides.
"""

import re
from bisect import bisect_left, bisect_right
from difflib import SequenceMatcher
from statistics import median

from rapidfuzz import fuzz

from kittum.core import Span, find_quote, new_id, sha256_hex
from kittum.documents.layout import rotate, unrotate
from kittum.documents.model import (
    Anchor,
    Box,
    CellRef,
    Layer,
    Page,
    PageAgreement,
    Resolution,
    TextQuote,
    Word,
)

RESOLVE_VERSION = "kittum.documents.resolve.v2"
CONTEXT_CHARS = 32
_SAME_PLACE = 0.5  # share of the old boxes' area the new boxes must cover
_FUZZY_MIN = 90.0
_FUZZY_MIN_LENGTH = 20
_CONTEXT_PROBE = 16  # non-space characters of context compared on each side


class DifferentDocument(ValueError):
    """An anchor was resolved against a layer of another document."""


def _word_starts(layer: Layer) -> list[int]:
    return [w.start for w in layer.words]


def _part_box(word: Word, start: int, end: int, page: Page | None) -> Box:
    """The box of characters [start, end) of a word, assuming even character widths.

    The word is cut along its reading direction: in the page's reading frame, then turned back
    to the unrotated frame, so a sideways word is cut along its height.
    """
    length = max(word.end - word.start, 1)
    a = (max(start, word.start) - word.start) / length
    b = (min(end, word.end) - word.start) / length
    frame, width, height = (page.frame, page.width, page.height) if page else (0, 0.0, 0.0)
    box = word.box
    x0, y0, x1, y1 = rotate((box.x0, box.y0, box.x1, box.y1), frame, width, height)
    x0, x1 = x0 + a * (x1 - x0), x0 + b * (x1 - x0)
    turned = (height, width) if frame % 180 else (width, height)
    x0, y0, x1, y1 = unrotate((x0, y0, x1, y1), frame, *turned)
    return box.model_copy(
        update={"x0": round(x0, 2), "y0": round(y0, 2), "x1": round(x1, 2), "y1": round(y1, 2)}
    )


def boxes_for(layer: Layer, start: int, end: int) -> tuple[Box, ...]:
    """One box per printed line (or cell line) that [start, end) touches."""
    starts = _word_starts(layer)
    i = max(bisect_right(starts, start) - 1, 0)
    line_starts = [ln.start for ln in layer.lines]
    pages = {p.number: p for p in layer.pages}
    merged: dict[int, Box] = {}
    order: list[int] = []
    while i < len(layer.words) and layer.words[i].start < end:
        word = layer.words[i]
        i += 1
        if word.end <= start:
            continue
        box = _part_box(word, start, end, pages.get(word.box.page))
        line = bisect_right(line_starts, word.start) - 1
        prev = merged.get(line)
        if prev is None:
            merged[line] = box
            order.append(line)
        else:
            merged[line] = prev.model_copy(
                update={
                    "x0": min(prev.x0, box.x0),
                    "y0": min(prev.y0, box.y0),
                    "x1": max(prev.x1, box.x1),
                    "y1": max(prev.y1, box.y1),
                }
            )
    return tuple(merged[k] for k in order)


def cells_for(layer: Layer, start: int, end: int) -> tuple[CellRef, ...]:
    return tuple(
        CellRef(
            table_id=c.table_id,
            row=c.row,
            col=c.col,
            row_span=c.row_span,
            col_span=c.col_span,
            header_path=c.header_path,
        )
        for c in layer.cells
        if c.start < end and start < c.end
    )


def make_anchor(layer: Layer, span: Span) -> Anchor:
    """An anchor for a `find_quote` span of `layer.text`."""
    if not 0 <= span.start < span.end <= len(layer.text):
        raise ValueError(f"span {span.start}..{span.end} is outside the layer text")
    exact = layer.text[span.start : span.end]
    return Anchor(
        id=new_id(),
        layer_id=layer.id,
        document_id=layer.document_id,
        quote=TextQuote(
            exact=exact,
            prefix=layer.text[max(0, span.start - CONTEXT_CHARS) : span.start],
            suffix=layer.text[span.end : span.end + CONTEXT_CHARS],
        ),
        text_hash=sha256_hex(exact.encode()),
        start=span.start,
        end=span.end,
        boxes=boxes_for(layer, span.start, span.end),
        cells=cells_for(layer, span.start, span.end),
    )


def _area(b: Box) -> float:
    return max(b.x1 - b.x0, 0.0) * max(b.y1 - b.y0, 0.0)


def _overlap(a: Box, b: Box) -> float:
    if a.page != b.page:
        return 0.0
    return max(min(a.x1, b.x1) - max(a.x0, b.x0), 0.0) * max(min(a.y1, b.y1) - max(a.y0, b.y0), 0.0)


def same_place(old: tuple[Box, ...], new: tuple[Box, ...]) -> bool:
    """Whether the new boxes cover most of the old boxes' area, page by page."""
    total = sum(_area(b) for b in old)
    if total == 0:
        return False
    covered = sum(min(sum(_overlap(o, n) for n in new), _area(o)) for o in old)
    return covered / total >= _SAME_PLACE


def _squash(text: str) -> str:
    return " ".join(text.split())


def _context_score(anchor: Anchor, candidate: Anchor) -> float:
    prefix = fuzz.ratio(_squash(anchor.quote.prefix)[-24:], _squash(candidate.quote.prefix)[-24:])
    suffix = fuzz.ratio(_squash(anchor.quote.suffix)[:24], _squash(candidate.quote.suffix)[:24])
    return (prefix + suffix) / 2


def _tight(text: str) -> str:
    return "".join(text.split())


def _context_agrees(anchor: Anchor, candidate: Anchor) -> bool:
    """Whether the text right before and after the quote is still exactly the same, up to
    `_CONTEXT_PROBE` non-space characters on each side (fewer at the document's ends).
    Whitespace is ignored, because engines separate words, lines, and cells differently. Any
    other difference ("-12" for "12", "fatal" for "nonfatal") makes the match a candidate."""
    n = _CONTEXT_PROBE
    old_prefix, new_prefix = _tight(anchor.quote.prefix)[-n:], _tight(candidate.quote.prefix)[-n:]
    old_suffix, new_suffix = _tight(anchor.quote.suffix)[:n], _tight(candidate.quote.suffix)[:n]
    return old_prefix == new_prefix and old_suffix == new_suffix


def _whole_tokens(text: str, span: Span) -> bool:
    """Whether a match starts and ends on token boundaries: "12" is not in "123", "x12",
    "12.5", or "3.12", and "Age" is not in "Ageing"."""

    def at(i: int) -> str:
        return text[i] if 0 <= i < len(text) else ""

    s, e = span.start, span.end
    if at(s).isalnum() and at(s - 1).isalnum():
        return False
    if at(e - 1).isalnum() and at(e).isalnum():
        return False
    if at(s).isdigit() and at(s - 1) in ".," and at(s - 2).isdigit():
        return False
    return not (at(e - 1).isdigit() and at(e) in ".," and at(e + 1).isdigit())


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+", text)


def _fuzzy(anchor: Anchor, layer: Layer) -> Anchor | None:
    """Best approximate match on the anchor's pages, for a quote the new layer spells differently
    (spacing, hyphens, ligatures). Its numbers must match exactly: a changed number is a
    different value, never a respelling."""
    needle = _squash(anchor.quote.exact)
    if len(needle) < _FUZZY_MIN_LENGTH:
        return None
    pages = {b.page for b in anchor.boxes}
    best: tuple[float, int, int] | None = None
    for page in layer.pages:
        if page.number not in pages:
            continue
        hay = layer.text[page.start : page.end]
        hit = fuzz.partial_ratio_alignment(needle, hay, score_cutoff=_FUZZY_MIN)
        if hit is not None and (best is None or hit.score > best[0]):
            best = (hit.score, page.start + hit.dest_start, page.start + hit.dest_end)
    if best is None:
        return None
    _, start, end = best
    while start < end and layer.text[start].isspace():
        start += 1
    while end > start and layer.text[end - 1].isspace():
        end -= 1
    # Widen to whole words: the alignment may start or end inside one.
    while start > 0 and not layer.text[start - 1].isspace():
        start -= 1
    while end < len(layer.text) and not layer.text[end].isspace():
        end += 1
    if end <= start or _numbers(layer.text[start:end]) != _numbers(needle):
        return None
    return make_anchor(layer, Span(start, end))


def resolve(anchor: Anchor, layer: Layer) -> Resolution:
    """Map an anchor onto another layer of the same document.

    A match must start and end on token boundaries, so a changed number never matches part of
    the new one.

    - `exact`: the quote occurs once at the same place, or several times but only once there,
      and the text around it is unchanged.
    - `candidate`: the quote occurs once elsewhere, at the same place with changed context, or
      approximately at the old place (`fuzzy`).
    - `ambiguous`: several matches and none singled out by place; `alternatives` lists them,
      best context first.
    - `unresolved`: no match.

    Raises `DifferentDocument` if the layer belongs to another document. A revised PDF is a
    different document; mapping onto it needs a document-revision mapping, which never carries
    acceptance across, so `resolve` refuses rather than returning a status that could.
    """

    def result(status, found: Anchor | None, method: str, alternatives=()) -> Resolution:
        return Resolution(
            status=status,
            anchor=found,
            method=method,
            method_version=RESOLVE_VERSION,
            alternatives=tuple(alternatives),
        )

    if layer.document_id != anchor.document_id:
        raise DifferentDocument(
            f"anchor {anchor.id} is on document {anchor.document_id}, "
            f"layer {layer.id} on document {layer.document_id}"
        )
    if layer.id == anchor.layer_id:
        if layer.text[anchor.start : anchor.end] == anchor.quote.exact:
            return result("exact", anchor, "same_layer")
        return result("unresolved", None, "same_layer")
    spans = [s for s in find_quote(anchor.quote.exact, layer.text) if _whole_tokens(layer.text, s)]
    found = [make_anchor(layer, s) for s in spans]

    def placed(match: Anchor) -> str:
        return "exact" if _context_agrees(anchor, match) else "candidate"

    if not found:
        guess = _fuzzy(anchor, layer)
        if guess is not None:
            return result("candidate", guess, "fuzzy")
        return result("unresolved", None, "none")
    if len(found) == 1:
        only = found[0]
        return result(
            placed(only) if same_place(anchor.boxes, only.boxes) else "candidate", only, "quote"
        )
    here = [f for f in found if same_place(anchor.boxes, f.boxes)]
    if len(here) == 1:
        return result(placed(here[0]), here[0], "quote_in_region")
    ranked = sorted(found, key=lambda f: -_context_score(anchor, f))
    return result("ambiguous", None, "quote", ranked)


def _iou(a: Box, b: Box) -> float:
    inter = _overlap(a, b)
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def compare_layers(a: Layer, b: Layer) -> tuple[PageAgreement, ...]:
    """Differential check of two layers of the same PDF, page by page.

    Words are aligned as sequences, so reading-order differences lower `text_similarity`. Boxes
    are compared only for words both layers read identically.
    """
    out: list[PageAgreement] = []
    starts_a, starts_b = _word_starts(a), _word_starts(b)
    for pa, pb in zip(a.pages, b.pages, strict=False):
        wa = a.words[bisect_left(starts_a, pa.start) : bisect_left(starts_a, pa.end)]
        wb = b.words[bisect_left(starts_b, pb.start) : bisect_left(starts_b, pb.end)]
        ta = [a.text[w.start : w.end] for w in wa]
        tb = [b.text[w.start : w.end] for w in wb]
        matcher = SequenceMatcher(None, ta, tb, autojunk=False)
        ious = [
            _iou(wa[i + k].box, wb[j + k].box)
            for i, j, n in matcher.get_matching_blocks()
            for k in range(n)
        ]
        out.append(
            PageAgreement(
                page=pa.number,
                words_a=len(wa),
                words_b=len(wb),
                text_similarity=round(matcher.ratio(), 4) if ta or tb else 1.0,
                box_iou=round(median(ious), 4) if ious else 0.0,
            )
        )
    return tuple(out)


def page_words(layer: Layer, page: int) -> list[tuple[Word, str]]:
    """The words on one page in text order, each with its text, for the viewer's text layer."""
    for p in layer.pages:
        if p.number == page:
            starts = _word_starts(layer)
            words = layer.words[bisect_left(starts, p.start) : bisect_left(starts, p.end)]
            return [(w, layer.text[w.start : w.end]) for w in words]
    raise KeyError(page)
