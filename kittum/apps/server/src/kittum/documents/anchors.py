"""Anchors into a layer, and re-anchoring onto a new layer of the same PDF.

`make_anchor` and `resolve` do no I/O; `documents.remap` stores what `resolve` decides.
"""

import re
from bisect import bisect_left, bisect_right
from difflib import SequenceMatcher
from statistics import median

from rapidfuzz import fuzz

from kittum.core import Span, find_quote, new_id, sha256_hex
from kittum.documents.model import (
    Anchor,
    Box,
    CellRef,
    Layer,
    PageAgreement,
    Resolution,
    TextQuote,
    Word,
)

RESOLVE_VERSION = "kittum.documents.resolve.v1"
CONTEXT_CHARS = 32
_SAME_PLACE = 0.5  # share of the old boxes' area the new boxes must cover
_FUZZY_MIN = 90.0
_FUZZY_MIN_LENGTH = 20


def _word_starts(layer: Layer) -> list[int]:
    return [w.start for w in layer.words]


def _part_box(word: Word, start: int, end: int) -> Box:
    """The box of characters [start, end) of a word, assuming even character widths."""
    length = max(word.end - word.start, 1)
    a = (max(start, word.start) - word.start) / length
    b = (min(end, word.end) - word.start) / length
    width = word.box.x1 - word.box.x0
    return word.box.model_copy(
        update={"x0": round(word.box.x0 + a * width, 2), "x1": round(word.box.x0 + b * width, 2)}
    )


def boxes_for(layer: Layer, start: int, end: int) -> tuple[Box, ...]:
    """One box per printed line (or cell line) that [start, end) touches."""
    starts = _word_starts(layer)
    i = max(bisect_right(starts, start) - 1, 0)
    line_starts = [ln.start for ln in layer.lines]
    merged: dict[int, Box] = {}
    order: list[int] = []
    while i < len(layer.words) and layer.words[i].start < end:
        word = layer.words[i]
        i += 1
        if word.end <= start:
            continue
        box = _part_box(word, start, end)
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
    """Map an anchor onto `layer`.

    - `exact`: the quote occurs once at the same place, or several times but only once there.
    - `candidate`: the quote occurs once elsewhere, or approximately at the old place (`fuzzy`).
    - `ambiguous`: several matches and none singled out by place; `alternatives` lists them,
      best context first.
    - `unresolved`: no match.
    """

    def result(status, found: Anchor | None, method: str, alternatives=()) -> Resolution:
        return Resolution(
            status=status,
            anchor=found,
            method=method,
            method_version=RESOLVE_VERSION,
            alternatives=tuple(alternatives),
        )

    if layer.id == anchor.layer_id:
        if layer.text[anchor.start : anchor.end] == anchor.quote.exact:
            return result("exact", anchor, "same_layer")
        return result("unresolved", None, "same_layer")
    spans = find_quote(anchor.quote.exact, layer.text)
    found = [make_anchor(layer, s) for s in spans]
    if not found:
        guess = _fuzzy(anchor, layer)
        if guess is not None:
            return result("candidate", guess, "fuzzy")
        return result("unresolved", None, "none")
    if len(found) == 1:
        only = found[0]
        return result(
            "exact" if same_place(anchor.boxes, only.boxes) else "candidate", only, "quote"
        )
    here = [f for f in found if same_place(anchor.boxes, f.boxes)]
    if len(here) == 1:
        return result("exact", here[0], "quote_in_region")
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
