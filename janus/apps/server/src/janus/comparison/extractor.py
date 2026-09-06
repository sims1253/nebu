"""Extracts field values from document evidence.

For each planned field, the locate rules score candidates — table cells for
`table_label`, label/value text lines for `text_label`, anchor-bounded runs of
prose for `text_span` — and the value rules pick the component out of the
winning candidate's text."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from functools import cache
from typing import Any

from rapidfuzz import fuzz

from janus.comparison.content import BoundingBox, PageContent
from janus.comparison.models import (
    ComparisonPlan,
    CompoundShape,
    DocumentEvidence,
    EvidenceLocation,
    ExtractionResult,
    ExtractionStatus,
    FieldExtraction,
    PlannedField,
    RowGeneratorRule,
    TextSpanLocator,
    field_identifier,
)


@dataclass(frozen=True)
class _Candidate:
    label: str
    value: str
    page: int
    table_index: int
    row: int
    column: int
    confidence: float
    cell_bbox: dict[str, float] | None
    table_bbox: dict[str, float] | None
    column_context: str | None
    row_context: str | None
    kind: str  # "table" or "text"


def _bbox(value: Any) -> dict[str, float] | None:
    if value is None:
        return None
    return {key: float(getattr(value, key)) for key in ("x", "y", "width", "height")}


def _table_candidates(evidence: DocumentEvidence) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for raw_page in evidence.pages:
        page = (
            raw_page if isinstance(raw_page, PageContent) else PageContent.model_validate(raw_page)
        )
        for table_index, table in enumerate(page.tables):
            cells = {(cell.row, cell.column): cell for cell in table.cells}
            rows = sorted({cell.row for cell in table.cells})
            columns = sorted({cell.column for cell in table.cells})
            headers = {
                column: cells[(0, column)].text.strip()
                for column in columns
                if table.header_row and (0, column) in cells
            }
            for row in rows:
                # Row 0 supplies column_context, but it is also a data
                # candidate: headerless tables (invoices, statements) put
                # real values in their first row, and a genuine header row
                # ("Value", "Characteristic") will not fuzz-match a field
                # label past the confidence threshold anyway.
                label_cell = cells.get((row, 0))
                if label_cell is None or not label_cell.text.strip():
                    continue
                for column in columns:
                    if column == 0:
                        continue
                    value_cell = cells.get((row, column))
                    if value_cell is None or not value_cell.text.strip():
                        continue
                    candidates.append(
                        _Candidate(
                            label=label_cell.text.strip(),
                            value=value_cell.text.strip(),
                            page=(
                                value_cell.bbox.page
                                if value_cell.bbox is not None
                                else page.page_number
                            ),
                            table_index=table_index,
                            row=row,
                            column=column,
                            confidence=min(label_cell.confidence, value_cell.confidence),
                            cell_bbox=_bbox(value_cell.bbox),
                            table_bbox=_bbox(table.bbox),
                            column_context=headers.get(column) or None,
                            row_context=label_cell.section,
                            kind="table",
                        )
                    )
    return candidates


# A text label is short and carries a real word; the colon on the line is the
# stronger signal, so unlike the form-line rebuild these guards only keep
# sentence clauses from posing as labels.
_TEXT_LABEL_MAX_CHARS = 64
_TEXT_LABEL_MAX_WORDS = 8
# A within-line value may be a whole sentence; a value married in from a
# neighbouring line is capped harder because prose columns sit side by side.
_TEXT_VALUE_MAX_CHARS = 200
_TEXT_CROSS_VALUE_MAX_CHARS = 80
_LETTERS = re.compile(r"[A-Za-z]{2}")
# Numbered section lines ("1. Identification" and friends) or short all-caps
# lines act as headings; a text candidate carries the nearest preceding one
# as its row context. The number must be followed by a word, so short value
# lines like "1 °C" stay values.
_NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Za-z]")
# Kerning can make a label slightly overlap the line that follows it.
_MAX_LINE_OVERLAP = 1.5
# A form line keeps its value close; a line far to the right belongs to
# another column of the page.
_VALUE_GAP_FACTOR = 0.25
_DEFAULT_PAGE_WIDTH = 612.0


def _label_like(label: str) -> bool:
    return (
        bool(label)
        and len(label) <= _TEXT_LABEL_MAX_CHARS
        and len(label.split()) <= _TEXT_LABEL_MAX_WORDS
        and bool(_LETTERS.search(label))
    )


def _is_heading(text: str) -> bool:
    if not text or len(text) > 64:
        return False
    if _NUMBERED_HEADING.match(text):
        return True
    # All-caps headings must stay letter-only: product names in caps
    # ("ACRYLIC ACID 90%") are values, not headings.
    return (
        text.isupper()
        and not any(c.isdigit() or c == "%" for c in text)
        and bool(re.search(r"[A-Z]{3}", text))
    )


@dataclass(frozen=True)
class _Line:
    text: str
    bbox: BoundingBox | None
    confidence: float
    context: str | None


def _page_lines(page: PageContent) -> list[_Line]:
    """The page's text elements as one entry per printed line, each stamped
    with the nearest preceding heading.

    The current reader stores line-level elements already; evidence written
    by older readers holds block-level elements, so their text is split into
    lines here (with the whole block's box, which is coarser but sound)."""
    lines: list[_Line] = []
    heading: str | None = None
    for element in page.text_elements:
        for raw in element.text.splitlines():
            text = raw.strip()
            if not text:
                continue
            if _is_heading(text):
                heading = text
            lines.append(
                _Line(text=text, bbox=element.bbox, confidence=element.confidence, context=heading)
            )
    return lines


def _split_label_value(text: str) -> tuple[str, str] | None:
    """A line that prints "Label: value" — the pair the text_label strategy
    is for. The first colon splits it; no colon or an empty value is not a
    pair."""
    label, separator, value = text.partition(":")
    if not separator:
        return None
    label, value = label.strip(), value.strip()
    if not value or len(value) > _TEXT_VALUE_MAX_CHARS or not _label_like(label):
        return None
    return label, value


def _shares_baseline(left: BoundingBox, right: BoundingBox) -> bool:
    # Larger type is set on a lower baseline, so the tolerance grows with the
    # smaller of the two line heights (mirroring the form-line pairing).
    tolerance = min(4.0, max(1.0, 0.3 * min(left.height, right.height)))
    return abs(left.y - right.y) <= tolerance


def _text_candidates(evidence: DocumentEvidence) -> list[_Candidate]:
    """Label/value pairs from the running text of every page.

    Two shapes pair up, whichever way the printer set them: a label and its
    value inside one line ("Product: ACRYLIC ACID 90% AQUEOUS"), or a label
    line whose value starts in a column to its right on the same baseline
    ("Boiling point:" beside "106 °C"), the same pairing the form-line
    rebuild uses for aligned spans. Values that wrap onto further lines keep
    only their first line.
    """
    candidates: list[_Candidate] = []
    for raw_page in evidence.pages:
        page = (
            raw_page if isinstance(raw_page, PageContent) else PageContent.model_validate(raw_page)
        )
        lines = _page_lines(page)
        page_width = page.width if page.width else _DEFAULT_PAGE_WIDTH
        paired: set[int] = {i for i, line in enumerate(lines) if _split_label_value(line.text)}
        for row, line in enumerate(lines):
            pair = _split_label_value(line.text)
            if pair is not None:
                label, value = pair
                cell_bbox, confidence = line.bbox, line.confidence
            else:
                # A label line that ends in a colon and holds no value of its
                # own takes the nearest value-like line to its right on the
                # same baseline.
                if not line.text.endswith(":") or not line.bbox:
                    continue
                label = line.text[:-1].strip()
                if not _label_like(label):
                    continue
                values = [
                    other
                    for index, other in enumerate(lines)
                    if index != row
                    and index not in paired
                    and other.bbox is not None
                    and other.bbox.x >= line.bbox.x + line.bbox.width - _MAX_LINE_OVERLAP
                    and other.bbox.x - (line.bbox.x + line.bbox.width)
                    <= _VALUE_GAP_FACTOR * page_width
                    and _shares_baseline(line.bbox, other.bbox)
                    and other.text
                    and not other.text.endswith(":")
                    and len(other.text) <= _TEXT_CROSS_VALUE_MAX_CHARS
                ]
                if not values:
                    continue
                value_line = min(values, key=lambda other: other.bbox.x if other.bbox else 0)
                label, value, cell_bbox, confidence = (
                    label,
                    value_line.text,
                    value_line.bbox,
                    min(line.confidence, value_line.confidence),
                )
            candidates.append(
                _Candidate(
                    label=label,
                    value=value,
                    page=cell_bbox.page if cell_bbox is not None else page.page_number,
                    # A text line is no table: row is the line's ordinal and
                    # column 1 the value side, column 0 being the label.
                    table_index=0,
                    row=row,
                    column=1,
                    confidence=confidence,
                    cell_bbox=_bbox(cell_bbox),
                    table_bbox=None,
                    column_context=None,
                    row_context=line.context,
                    kind="text",
                )
            )
    return candidates


# A span anchor is a structural boundary — a heading or a short lead-in line —
# never a sentence that merely mentions the alias words, so anchor candidates
# stay as short as a heading can be.
_SPAN_ANCHOR_MAX_CHARS = 64
# A running head repeats at the same height on page after page; two pages of
# agreement at (roughly) the same y are enough to call one. The bucket rounds
# y to this tolerance, so heads a point apart still land together.
_RUNNING_Y_TOLERANCE = 3.0
# Chrome lives in the page margins; a block genuinely duplicated mid-page is
# content, not furniture, so only margin lines are running-head candidates.
# Without a page height nothing can be verified and nothing is stripped.
_RUNNING_MARGIN_FRACTION = 0.12
# A page-number folio: short, letter-free, at least one digit ("4", "- 7 -").
_FOLIO_LINE = re.compile(r"^[\d\s.,;:()/\u2013\u2014-]*\d[\d\s.,;:()/\u2013\u2014-]*$")


@dataclass
class _SpanLine:
    """One printed line in document reading order, for the text_span pass."""

    text: str
    page: int
    row: int  # ordinal within the page, top to bottom
    bbox: BoundingBox | None
    page_height: float | None
    confidence: float
    context: str | None  # nearest preceding heading, carried across pages
    running: bool  # a repeated running head or folio; invisible to spans


def _is_folio(text: str) -> bool:
    return len(text) <= 8 and bool(_FOLIO_LINE.fullmatch(text))


def _in_margin(line: _SpanLine) -> bool:
    if line.bbox is None or not line.page_height:
        return False
    return (
        line.bbox.y <= line.page_height * _RUNNING_MARGIN_FRACTION
        or line.bbox.y >= line.page_height * (1 - _RUNNING_MARGIN_FRACTION)
    )


def _mark_running_lines(lines: list[_SpanLine], page_count: int) -> None:
    """Flag margin lines that repeat at the same height on two or more pages.

    Those are the running heads and page-number folios no article text wants
    in its prose. A repeated line stays usable as a (repeated) anchor; it just
    never contributes to a span and never ends one."""
    if page_count < 2:
        return
    candidates = [
        (line, box) for line in lines if _in_margin(line) and (box := line.bbox) is not None
    ]
    by_text: dict[tuple[int, str], set[int]] = {}
    folio_heights: dict[int, set[int]] = {}
    for line, box in candidates:
        bucket = round(box.y / _RUNNING_Y_TOLERANCE)
        by_text.setdefault((bucket, _folded(line.text)), set()).add(line.page)
        if _is_folio(line.text):
            folio_heights.setdefault(bucket, set()).add(line.page)
    repeated = {key for key, pages in by_text.items() if len(pages) >= 2}
    folio_at = {bucket for bucket, pages in folio_heights.items() if len(pages) >= 2}
    for line, box in candidates:
        bucket = round(box.y / _RUNNING_Y_TOLERANCE)
        line.running = (bucket, _folded(line.text)) in repeated or (
            _is_folio(line.text) and bucket in folio_at
        )


def _document_lines(evidence: DocumentEvidence) -> list[_SpanLine]:
    """Every line of every page in reading order, for the text_span strategy.

    Lines run page by page, top to bottom (y, then x; a stable sort, so lines
    sharing a box keep their printed order), each stamped with the nearest
    preceding heading — carried across pages, because a section that spans a
    page break keeps its heading. Repeated running heads are then flagged."""
    lines: list[_SpanLine] = []
    heading: str | None = None
    for raw_page in evidence.pages:
        page = (
            raw_page if isinstance(raw_page, PageContent) else PageContent.model_validate(raw_page)
        )
        page_lines: list[_SpanLine] = []
        for element in page.text_elements:
            for printed in element.text.splitlines():
                text = printed.strip()
                if not text:
                    continue
                page_lines.append(
                    _SpanLine(
                        text=text,
                        page=page.page_number,
                        row=0,
                        bbox=element.bbox,
                        page_height=page.height,
                        confidence=element.confidence,
                        context=None,
                        running=False,
                    )
                )
        page_lines.sort(
            key=lambda line: (
                line.bbox.y if line.bbox is not None else math.inf,
                line.bbox.x if line.bbox is not None else math.inf,
            )
        )
        for row, line in enumerate(page_lines):
            line.row = row
            if _is_heading(line.text):
                heading = line.text
            line.context = heading
        lines.extend(page_lines)
    _mark_running_lines(lines, len(evidence.pages))
    return lines


def _heading_level(text: str) -> int | None:
    """The outline level of a heading line: the depth of a numbered heading
    ("2.1 Measures" -> 2), 1 for a short all-caps heading, None for a line
    that is not a heading at all."""
    if not _is_heading(text):
        return None
    numbered = re.match(r"(\d+(?:\.\d+)*)", text)
    return len(numbered.group(1).split(".")) if numbered else 1


def _matches_end_anchor(aliases: list[str], text: str, minimum_confidence: float) -> bool:
    """Whether a line closes the span. Whole-line similarity, not token-set
    containment: the end anchor must BE the printed heading, so a sentence
    that merely mentions "references" cannot end a span early."""
    folded = text.casefold()
    return any(
        fuzz.ratio(alias.casefold(), folded) / 100 >= minimum_confidence for alias in aliases
    )


@dataclass(frozen=True)
class _TableZone:
    """One detected table's rectangular claim on a page.

    `rect` is the table's own bbox, or the union of its cell bboxes when it
    has none; None when neither exists (a rebuilt borderless table whose
    cells carry no boxes). `certain` is whether the table's rules were found
    geometrically: only certain zones may exclude span lines, because the
    borderless fallback also "finds" tables in running prose, and an inferred
    rectangle must never swallow the prose it was mistaken for."""

    page: int
    rect: tuple[float, float, float, float] | None
    certain: bool


def _table_zones(evidence: DocumentEvidence) -> list[_TableZone]:
    """Every detected table as an exclusion zone for text spans."""
    zones: list[_TableZone] = []
    for raw_page in evidence.pages:
        page = (
            raw_page if isinstance(raw_page, PageContent) else PageContent.model_validate(raw_page)
        )
        for table in page.tables:
            if table.bbox is not None:
                box = table.bbox
                rect = (box.x, box.y, box.x + box.width, box.y + box.height)
            else:
                boxes = [cell.bbox for cell in table.cells if cell.bbox is not None]
                rect = (
                    (
                        min(box.x for box in boxes),
                        min(box.y for box in boxes),
                        max(box.x + box.width for box in boxes),
                        max(box.y + box.height for box in boxes),
                    )
                    if boxes
                    else None
                )
            zones.append(
                _TableZone(page=page.page_number, rect=rect, certain=table.bordered is True)
            )
    return zones


def _zones_by_page(
    zones: list[_TableZone],
) -> tuple[dict[int, list[tuple[int, _TableZone]]], dict[int, set[int]]]:
    """The zones indexed by page: bounded zones (with their ordinal) for the
    per-line containment test, and the ordinals of zone-less tables — tables
    with no locatable rectangle at all — for the kept-lines note."""
    bounded: dict[int, list[tuple[int, _TableZone]]] = {}
    unbounded: dict[int, set[int]] = {}
    for index, zone in enumerate(zones):
        if zone.rect is None:
            unbounded.setdefault(zone.page, set()).add(index)
        else:
            bounded.setdefault(zone.page, []).append((index, zone))
    return bounded, unbounded


def _inside_zone(line: _SpanLine, zone: _TableZone) -> bool:
    """Whether a printed line falls inside the zone's rectangle; a point or
    two of slack absorbs kerning and line overlap (mirroring the form-line
    pairing tolerance). A line without a box cannot be placed and stays."""
    if zone.rect is None or line.bbox is None:
        return False
    x0, y0, x1, y1 = zone.rect
    box = line.bbox
    return (
        box.x >= x0 - _MAX_LINE_OVERLAP
        and box.x + box.width <= x1 + _MAX_LINE_OVERLAP
        and box.y >= y0 - _MAX_LINE_OVERLAP
        and box.y + box.height <= y1 + _MAX_LINE_OVERLAP
    )


@dataclass
class _SpanReport:
    """What table exclusion did to one span: how many lines were kept out as
    ruled-table content, and which unexcludable tables (borderless inferences
    and tables with no locatable rectangle) kept their lines inside it."""

    excluded: int = 0
    unexcludable: set[int] = dataclass_field(default_factory=set)  # zone ordinals


def _span_lines(
    locator: TextSpanLocator,
    lines: list[_SpanLine],
    anchor_index: int,
    zones: list[_TableZone] | None = None,
) -> tuple[list[_SpanLine], _SpanLine | None, _SpanReport]:
    """The content of the span that starts after `lines[anchor_index]`, plus
    the line that ends it (None when the span runs to the document's end) and
    a report of what table exclusion did.

    An explicit `end_labels` closes the span before the first line that
    matches an alias. Without one, the span closes before the next heading of
    the same or higher level than its anchor — the anchor counts as level 1
    when it is not a heading itself. Running heads never contribute and never
    close. With `exclude_tables` (the default), a line inside a geometrically
    ruled table is span furniture, not span prose: it never contributes to
    the span's content — though it can still close it as an end anchor,
    because exclusion polices content, not boundaries."""
    anchor_level = _heading_level(lines[anchor_index].text) or 1
    bounded, unbounded = _zones_by_page(zones) if locator.exclude_tables and zones else ({}, {})
    report = _SpanReport()
    content: list[_SpanLine] = []
    for line in lines[anchor_index + 1 :]:
        if line.running:
            continue
        if locator.end_labels:
            if _matches_end_anchor(locator.end_labels, line.text, locator.minimum_confidence):
                return content, line, report
        else:
            level = _heading_level(line.text)
            if level is not None and level <= anchor_level:
                return content, line, report
        excluded = False
        for index, zone in bounded.get(line.page, ()):
            if not _inside_zone(line, zone):
                continue
            if zone.certain:
                excluded = True
            else:
                # A borderless inference whose lines stay: noted, so a table
                # the author wanted out is never dropped silently.
                report.unexcludable.add(index)
        if excluded:
            report.excluded += 1
            continue
        # A table with no locatable rectangle cannot be excluded either; a
        # page that contributes prose notes every such table sitting on it.
        report.unexcludable.update(unbounded.get(line.page, set()))
        content.append(line)
    return content, None, report


def _span_text(content: list[_SpanLine]) -> str:
    return " ".join(line.text for line in content)


def _anchor_candidate(line: _SpanLine) -> _Candidate:
    # Adapter so the shared scorer reads a span anchor like any other
    # candidate: the line's text is the label, its heading context the row
    # context. value stays empty — the span, not the line, is the value.
    return _Candidate(
        label=line.text,
        value="",
        page=line.page,
        table_index=0,
        row=line.row,
        column=0,
        confidence=line.confidence,
        cell_bbox=None,
        table_bbox=None,
        column_context=None,
        row_context=line.context,
        kind="span",
    )


def _union_bbox(content: list[_SpanLine]) -> dict[str, float] | None:
    """The bounding region of a single-page span; None when any line has no
    box (a rectangle over unknown positions would invent evidence)."""
    boxes = [line.bbox for line in content if line.bbox is not None]
    if len(boxes) != len(content) or not boxes:
        return None
    x = min(box.x for box in boxes)
    y = min(box.y for box in boxes)
    right = max(box.x + box.width for box in boxes)
    bottom = max(box.y + box.height for box in boxes)
    return {"x": x, "y": y, "width": right - x, "height": bottom - y}


def _tokens(text: str) -> frozenset[str]:
    return frozenset(text.casefold().split())


def _carries_token_set(needles: list[str], haystack: str) -> bool:
    """Whether some needle appears in the haystack as a complete token set.

    Token-set similarity barely registers a single substituted token, so
    "H2: Effort preferences ..." scores 97 against the row labelled
    "H4: Effort preferences ...". Containment is the exact counterpart: the
    requested words are all present, or they are not.
    """
    tokens = _tokens(haystack)
    return any(_tokens(needle) <= tokens for needle in needles)


def _column_labels(field: PlannedField) -> list[str]:
    # Only the table_label strategy has columns; text runs have none.
    return getattr(field.locate, "column_labels", [])


def _is_complete_match(field: PlannedField, candidate: _Candidate) -> bool:
    """Whether the candidate carries the requested label, and the requested
    column context, as complete token sets rather than approximations."""
    if not _carries_token_set(field.locate.labels, candidate.label):
        return False
    return _is_complete_column_match(field, candidate)


def _is_complete_column_match(field: PlannedField, candidate: _Candidate) -> bool:
    """Check column-token containment when column labels are specified.

    Text locators have no columns, so only their label completeness matters."""
    columns = _column_labels(field)
    if not columns:
        return True
    return _carries_token_set(columns, candidate.column_context or "")


@dataclass(frozen=True)
class _ScoreBreakdown:
    """Candidate score factors. Label is None for pattern-generated rows."""

    label: float | None
    section_factor: float
    column_factor: float
    confidence: float
    final: float

    def sentence(self, minimum_confidence: float | None = None) -> str:
        """Format the score factors and optional confidence threshold."""
        label = "pattern" if self.label is None else f"label {self.label:.2f}"
        floor = f", floor {minimum_confidence:.2f}" if minimum_confidence is not None else ""
        return (
            f"score: {label} x section {self.section_factor:.2f} x "
            f"column {self.column_factor:.2f} x confidence {self.confidence:.2f} "
            f"= {self.final:.2f}{floor}"
        )


def _section_factor(field: PlannedField, candidate: _Candidate) -> float:
    # Section context can cut the label score by at most 30 percent; it never
    # raises it.
    if not field.locate.section_labels:
        return 1.0
    context = (candidate.row_context or "").casefold()
    section_score = max(
        fuzz.token_set_ratio(label.casefold(), context) / 100
        for label in field.locate.section_labels
    )
    return 0.7 + 0.3 * section_score


def _column_factor(field: PlannedField, candidate: _Candidate) -> float:
    # Column context can cut the label score by at most 30 percent; it never
    # raises it.
    if not _column_labels(field):
        return 1.0
    context = (candidate.column_context or "").casefold()
    column_score = max(
        fuzz.token_set_ratio(label.casefold(), context) / 100 for label in _column_labels(field)
    )
    return 0.7 + 0.3 * column_score


def _detailed_score(field: PlannedField, candidate: _Candidate) -> _ScoreBreakdown:
    label_score = max(
        fuzz.token_set_ratio(alias.casefold(), candidate.label.casefold()) / 100
        for alias in field.locate.labels
    )
    section = _section_factor(field, candidate)
    column = _column_factor(field, candidate)
    return _ScoreBreakdown(
        label=label_score,
        section_factor=section,
        column_factor=column,
        confidence=candidate.confidence,
        final=label_score * section * column * candidate.confidence,
    )


def _folded(value: str) -> str:
    return " ".join(value.casefold().split())


_NUMBER = r"[-+]?\d+(?:[.,]\d+)?"
# A compared number inside a trailing annotation; unlike _NUMBER it also
# accepts a bare decimal separator (".025" in "P=.025").
_ANNOTATED_NUMBER = r"[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
# Trailing annotations that share a cell with a statistic, as in
# "0.26 [0.22, 0.31]; P<0.001; BF>100". Each repetition is one short token
# (P, BF, d, ...) compared against a number.
_STATISTIC_ANNOTATION = (
    r"(?:\s*[,;]\s*[A-Za-z][A-Za-z0-9]{0,4}\s*[<>=\u2264\u2265]\s*"
    rf"{_ANNOTATED_NUMBER}\s*)*"
)
# The statistic annotations that are extractable as components. The captured
# group is the raw annotation AFTER the P/BF token — the inequality and the
# number exactly as printed ("<0.001", "=.025") — because an inequality is an
# interval statement, not a value, and parsing it into a number would invent
# precision the document never stated.
_ANNOTATION_COMPONENTS: dict[str, re.Pattern[str]] = {
    component: re.compile(rf"\b{token}\s*([<>=\u2264\u2265]\s*{_ANNOTATED_NUMBER})", re.IGNORECASE)
    for component, token in (("p_value", "P"), ("bayes_factor", "BF"))
}
# Typeset statistics can carry a space after the decimal separator
# ("0. 261"); closing it keeps such cells parsable.
_DECIMAL_GAP = re.compile(r"(?<=[.,])\s+(?=\d)")

# One shape can admit several spellings of the same components; the first
# matching alternative wins, so the most specific form comes first.
_COMPOUND_PATTERNS: dict[CompoundShape, tuple[tuple[re.Pattern[str], tuple[str, ...]], ...]] = {
    CompoundShape.COUNT_PERCENTAGE: (
        (re.compile(rf"^\s*({_NUMBER})\s*\(\s*({_NUMBER})\s*%?\s*\)\s*$"), ("count", "percentage")),
    ),
    CompoundShape.MEAN_STANDARD_DEVIATION: (
        # mean with its error, as in: 0.261 (0.025)
        (
            re.compile(rf"^\s*({_NUMBER})\s*\(\s*({_NUMBER})\s*\)\s*{_STATISTIC_ANNOTATION}$"),
            ("mean", "standard_deviation"),
        ),
        # mean and error ahead of a bracketed interval, e.g. 0.261 (0.025) [0.213, 0.310]
        (
            re.compile(
                rf"^\s*({_NUMBER})\s*\(\s*({_NUMBER})\s*\)\s*"
                rf"\[\s*{_NUMBER}\s*,\s*{_NUMBER}\s*\]\s*{_STATISTIC_ANNOTATION}$"
            ),
            ("mean", "standard_deviation"),
        ),
    ),
    CompoundShape.RANGE: (
        (
            re.compile(
                rf"^\s*({_NUMBER})\s*[\u2013\u2014-]\s*({_NUMBER})\s*{_STATISTIC_ANNOTATION}$"
            ),
            ("minimum", "maximum"),
        ),
    ),
    CompoundShape.ESTIMATE_INTERVAL: (
        # estimate with error and bracketed interval, e.g. 0.261 (0.025) [0.213, 0.310]
        (
            re.compile(
                rf"^\s*({_NUMBER})\s*\(\s*({_NUMBER})\s*\)\s*"
                rf"\[\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\]\s*{_STATISTIC_ANNOTATION}$"
            ),
            ("estimate", "standard_error", "lower", "upper"),
        ),
        # estimate with a bracketed interval only, e.g. 0.26 [0.22, 0.31]
        (
            re.compile(
                rf"^\s*({_NUMBER})\s*\[\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\]\s*{_STATISTIC_ANNOTATION}$"
            ),
            ("estimate", "lower", "upper"),
        ),
        # estimate with a parenthesized interval, as in: 0.26 (0.22, 0.31)
        (
            re.compile(
                rf"^\s*({_NUMBER})\s*\(\s*({_NUMBER})\s*[,;\u2013\u2014-]\s*({_NUMBER})\s*\)"
                rf"\s*{_STATISTIC_ANNOTATION}$"
            ),
            ("estimate", "lower", "upper"),
        ),
    ),
}


@cache
def _compiled_value_pattern(pattern: str) -> re.Pattern[str]:
    """Cache compiled value patterns across candidate checks."""
    return re.compile(pattern)


# Characters a derived row key may carry — the field-key charset, so a
# generated "<field.key>.<row_key>" reads as one key. Anything else collapses
# to a single underscore.
_ROW_KEY_INVALID = re.compile(r"[^A-Za-z0-9_.-]+")
_ROW_KEY_MAX_CHARS = 96


def _sanitize_row_key(raw: str) -> str:
    """The captured key fragment as a row key: non-key characters collapse to
    one underscore, leading and trailing separators drop (a key starts with a
    letter or digit), and 96 characters cap the length. An empty result
    becomes "row" — a row was matched, so it keeps a key."""
    key = _ROW_KEY_INVALID.sub("_", raw.strip()).strip("_.-")
    return key[:_ROW_KEY_MAX_CHARS] or "row"


def _row_breakdown(field: PlannedField, candidate: _Candidate) -> _ScoreBreakdown:
    """The context score of a pattern-matched row: no alias scoring (the
    rows.label_pattern selected the row), but section and column context keep
    their multiplicative penalties and the reader confidence stays a factor —
    the same shape as the label score minus the fuzzy label term."""
    section = _section_factor(field, candidate)
    column = _column_factor(field, candidate)
    return _ScoreBreakdown(
        label=None,
        section_factor=section,
        column_factor=column,
        confidence=candidate.confidence,
        final=candidate.confidence * section * column,
    )


def _column_name(candidate: _Candidate) -> str:
    """How an explanation names a value column: its header, or its ordinal
    when the table is headerless."""
    return candidate.column_context or f"column {candidate.column}"


def _short(text: str, limit: int = 60) -> str:
    """An identifying text compact enough to quote inside one explanation
    sentence; the artifact keeps the untruncated form."""
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _rival_where(candidate: _Candidate) -> str:
    """Where an explanation places the runner-up: a table cell by row and
    column, a text line or span anchor by its line ordinal."""
    if candidate.kind == "table":
        return f"page {candidate.page}, row {candidate.row}, column {_column_name(candidate)!r}"
    return f"page {candidate.page}, line {candidate.row}"


@dataclass(frozen=True)
class _Rival:
    """The runner-up of a contested outcome: its score breakdown, the
    candidate itself, and the text that identifies it — the row label a
    static field tied on, the column header a rows field tied on, or the
    anchor line a span field tied on. Enough for both the explanation
    sentence and the artifact fields, so an ambiguous outcome names WHO it
    lost to, not just the tied scores."""

    breakdown: _ScoreBreakdown
    candidate: _Candidate
    text: str

    def clause(self) -> str:
        """The rival as one compact fragment: identifying text and location."""
        return f"{_short(self.text)!r} at {_rival_where(self.candidate)}"


def _rival_fields(rival: _Rival) -> dict[str, Any]:
    """The artifact's runner-up fields: its identifying text in full and its
    location, so the tie can be inspected without re-reading the evidence."""
    candidate = rival.candidate
    return {
        "runner_up_text": rival.text,
        "runner_up_location": EvidenceLocation(
            page=candidate.page,
            table_index=candidate.table_index,
            row=candidate.row,
            column=candidate.column,
            raw_text=rival.text,
            cell_bbox=candidate.cell_bbox,
            table_bbox=candidate.table_bbox,
        ),
    }


def _extract_component(field: PlannedField, raw: str) -> tuple[str | None, str | None]:
    shape = field.value.source_shape
    if shape is None:
        return raw, None
    text = _DECIMAL_GAP.sub("", raw)
    component = field.value.component or ""
    if component in _ANNOTATION_COMPONENTS:
        # An annotation component rides a cell that still matches the declared
        # shape; the shape patterns tolerate (and skip) the annotation tail.
        if not any(pattern.match(text) for pattern, _ in _COMPOUND_PATTERNS[shape]):
            return None, f"Value did not match the declared {shape.value} source shape"
        annotation = _ANNOTATION_COMPONENTS[component].search(text)
        if annotation is None:
            return None, f"The cell carries no {component} annotation to extract."
        return annotation.group(1), None
    for pattern, names in _COMPOUND_PATTERNS[shape]:
        match = pattern.match(text)
        if match is not None:
            components = dict(zip(names, match.groups(), strict=True))
            value = components.get(component)
            if value is None:
                # The cell matched a variant of the shape that does not carry
                # the requested component (a bare interval offers no standard
                # error): loud, not a KeyError.
                return None, (
                    f"The matched {shape.value} cell carries no {component}; it offers "
                    f"{', '.join(names)} only."
                )
            return value, None
    return None, (f"Value did not match the declared {shape.value} source shape")


_CANDIDATE_NOUN = {"table_label": "table label", "text_label": "text label"}
_LOCATION_NOUN = {"table_label": "table cell", "text_label": "text line"}


class SchemaExtractor:
    """Finds each field's value in the document's evidence.

    A `table_label` field scores the label/value cells of detected tables; a
    `text_label` field scores label/value pairs from the running text; a
    `text_span` field scores anchor lines and takes the prose that follows the
    winner, up to its end anchor. The best candidate above the configured
    confidence wins. A field with a `rows` generator instead yields one
    extraction per row label matching the generator's pattern, each generated
    from that label's best-scoring value column."""

    # When the runner-up sits at a different location and scores within this
    # gap of the winner, the field is reported ambiguous rather than guessed.
    ambiguity_gap = 0.03

    def extract(self, plan: ComparisonPlan, evidence: DocumentEvidence) -> ExtractionResult:
        pools: dict[str, list[_Candidate]] = {}
        document_lines: list[_SpanLine] | None = None
        table_zones: list[_TableZone] | None = None
        extractions: list[FieldExtraction] = []
        for field in plan.fields:
            if field.rows is not None:
                # A rows field is not one static definition: generate one
                # extraction per matching row label, in document order.
                if field.locate.strategy not in pools:
                    pools[field.locate.strategy] = (
                        _table_candidates(evidence)
                        if field.locate.strategy == "table_label"
                        else _text_candidates(evidence)
                    )
                extractions.extend(
                    self._extract_rows(
                        plan.specification_id, field, field.rows, pools[field.locate.strategy]
                    )
                )
                continue
            if isinstance(field.locate, TextSpanLocator):
                if document_lines is None:
                    document_lines = _document_lines(evidence)
                if table_zones is None:
                    table_zones = _table_zones(evidence)
                extractions.append(
                    self._extract_text_span(field, field.locate, document_lines, table_zones)
                )
                continue
            if field.locate.strategy not in pools:
                pools[field.locate.strategy] = (
                    _table_candidates(evidence)
                    if field.locate.strategy == "table_label"
                    else _text_candidates(evidence)
                )
            pool = pools[field.locate.strategy]
            noun = _CANDIDATE_NOUN[field.locate.strategy]
            ranked = sorted(
                ((_detailed_score(field, candidate), candidate) for candidate in pool),
                key=lambda item: item[0].final,
                reverse=True,
            )
            if not ranked or ranked[0][0].final < field.locate.minimum_confidence:
                best_breakdown = ranked[0][0] if ranked else None
                extractions.append(
                    FieldExtraction(
                        field_id=field.id,
                        field_key=field.key,
                        section_key=field.section_key,
                        status=ExtractionStatus.NOT_EXTRACTED,
                        explanation=(
                            f"No {noun} candidate met the configured confidence threshold"
                            + (
                                f"; best {best_breakdown.sentence(field.locate.minimum_confidence)}."
                                if best_breakdown is not None
                                else "."
                            )
                        ),
                        **(
                            self._score_fields(best_breakdown, field)
                            if best_breakdown is not None
                            else {}
                        ),
                    )
                )
                continue
            best_breakdown, best = ranked[0]
            best_score = best_breakdown.final
            agreed_tie = False
            if len(ranked) > 1:
                second_breakdown, second = ranked[1]
                second_score = second_breakdown.final
                same_score = second_score >= best_score - self.ambiguity_gap
                distinct_location = (best.page, best.table_index, best.row, best.column) != (
                    second.page,
                    second.table_index,
                    second.row,
                    second.column,
                )
                # A near tie is only a real tie when the runner-up matches the
                # requested label and column as completely as the winner. Rows
                # that differ by one token ("H2: ..." against "H4: ...") sit
                # inside the gap on similarity alone — and so does a subset
                # rival, a shorter line whose tokens all sit inside the alias.
                # The tie resolves toward whichever of the two carries the
                # alias completely: equal scores keep document order, so the
                # complete carrier can sit below its subset rival.
                resolved = False
                best_complete = _is_complete_match(field, best)
                second_complete = _is_complete_match(field, second)
                if same_score and distinct_location and best_complete != second_complete:
                    resolved = True
                    if second_complete:
                        best_breakdown, best = second_breakdown, second
                        best_score = best_breakdown.final
                        second_breakdown, second = ranked[0]
                        second_score = second_breakdown.final
                if same_score and distinct_location and not resolved and best.kind == "text":
                    # Running text repeats a header on every page by design.
                    # Tied text lines that print the same value agree, so the
                    # value is not a guess; duplicated table rows stay
                    # ambiguous.
                    tied = [
                        candidate
                        for breakdown, candidate in ranked
                        if breakdown.final >= best_score - self.ambiguity_gap
                    ]
                    agreed_tie = len(tied) > 1 and all(
                        _folded(candidate.value) == _folded(best.value) for candidate in tied
                    )
                    resolved = agreed_tie
                if same_score and distinct_location and not resolved:
                    # Name the rival, not just its score: the tied scores say
                    # how close the race was, the identifying text and
                    # location say which label to disambiguate against.
                    rival = _Rival(second_breakdown, second, second.label)
                    extractions.append(
                        FieldExtraction(
                            field_id=field.id,
                            field_key=field.key,
                            section_key=field.section_key,
                            status=ExtractionStatus.AMBIGUOUS,
                            confidence=best_score,
                            explanation=(
                                f"Multiple {_LOCATION_NOUN[field.locate.strategy]}s have equally "
                                f"strong label evidence; best "
                                f"{best_breakdown.sentence(field.locate.minimum_confidence)}, "
                                f"runner-up {rival.clause()} at {second_score:.2f} inside the "
                                f"{self.ambiguity_gap:.2f} ambiguity gap — column_labels or "
                                "section_labels must separate them."
                            ),
                            **self._score_fields(best_breakdown, field, rival),
                        )
                    )
                    continue
            # A known shape (id, url, date) is part of the locate contract: a
            # best candidate whose value breaks the shape is a wrong match,
            # and rejecting it here is loud instead of silent.
            if field.locate.value_pattern is not None and (
                _compiled_value_pattern(field.locate.value_pattern).search(best.value) is None
            ):
                # `second` names the runner-up — after a token-set promotion it
                # is the demoted subset rival, not the score-ranked number two.
                rival = _Rival(second_breakdown, second, second.label) if len(ranked) > 1 else None
                extractions.append(
                    FieldExtraction(
                        field_id=field.id,
                        field_key=field.key,
                        section_key=field.section_key,
                        status=ExtractionStatus.NOT_EXTRACTED,
                        raw_document_value=best.value,
                        confidence=best_score,
                        explanation=(
                            f"The best {noun} candidate's value {best.value!r} does not "
                            f"match locate.value_pattern {field.locate.value_pattern!r}; "
                            f"{best_breakdown.sentence(field.locate.minimum_confidence)}"
                            + (
                                f", runner-up {rival.clause()} at {rival.breakdown.final:.2f}."
                                if rival
                                else "."
                            )
                        ),
                        **self._score_fields(best_breakdown, field, rival),
                    )
                )
                continue
            component, component_error = _extract_component(field, best.value)
            if component is None:
                extractions.append(
                    FieldExtraction(
                        field_id=field.id,
                        field_key=field.key,
                        section_key=field.section_key,
                        status=ExtractionStatus.NOT_EXTRACTED,
                        raw_document_value=best.value,
                        confidence=best_score,
                        explanation=(
                            f"{component_error or 'The document value could not be parsed.'} "
                            f"{best_breakdown.sentence(field.locate.minimum_confidence)}."
                        ),
                        **self._score_fields(best_breakdown, field),
                    )
                )
                continue
            location = EvidenceLocation(
                page=best.page,
                table_index=best.table_index,
                row=best.row,
                column=best.column,
                raw_text=best.value,
                cell_bbox=best.cell_bbox,
                table_bbox=best.table_bbox,
            )
            explanation = (
                f"Selected {_LOCATION_NOUN[field.locate.strategy]} using {field.locate.strategy} "
                f"with confidence {best_score:.3f} "
                f"({best_breakdown.sentence(field.locate.minimum_confidence)})."
            )
            if agreed_tie:
                explanation += " Tied lines carried the same value."
            extractions.append(
                FieldExtraction(
                    field_id=field.id,
                    field_key=field.key,
                    section_key=field.section_key,
                    status=ExtractionStatus.EXTRACTED,
                    raw_document_value=best.value,
                    normalized_document_value=component,
                    location=location,
                    confidence=best_score,
                    explanation=explanation,
                    column_context=best.column_context,
                    row_context=best.row_context,
                    **self._score_fields(best_breakdown, field),
                )
            )
        return ExtractionResult(
            specification_id=plan.specification_id,
            extraction_hash=plan.extraction_hash,
            document_hash=evidence.document_hash,
            fields=extractions,
        )

    def _extract_rows(
        self,
        specification_id: str,
        field: PlannedField,
        generator: RowGeneratorRule,
        pool: list[_Candidate],
    ) -> list[FieldExtraction]:
        """Generate one extraction per matched row LABEL, in document (pool)
        order.

        The pattern replaces alias scoring as the row selector — a single
        field would have to pick one winning candidate, a rows field keeps
        every distinct-keyed match. A label that survives in several value
        columns generates from only its best-scoring column: the per-label
        counterpart of a static field's pool-wide choice, made inside the
        label, so `column_labels` genuinely selects the column instead of
        penalizing off-columns that still pass `minimum_confidence`. Columns
        that tie within the ambiguity gap make the row `ambiguous` rather
        than guessed — resolved exactly when the winner carries the requested
        column_labels completely and the rival does not, the same resolution
        a static field applies to near-identical headers. A
        locate.value_pattern joins the selector (a candidate whose value
        breaks the declared shape is not a row of this table; a reference key
        that loses its row that way surfaces as ROW_NOT_IN_DOCUMENT at
        comparison). The cap fires loudly when more rows match than max_rows
        allows, and key collisions get a numeric suffix so no row silently
        disappears."""
        pattern = _compiled_value_pattern(generator.label_pattern)
        # Only the table_label and text_label strategies carry a
        # value_pattern (rows is rejected for text_span at the schema).
        shape = getattr(field.locate, "value_pattern", None)
        shape_pattern = _compiled_value_pattern(shape) if shape is not None else None
        pattern_matched: list[tuple[_Candidate, str, _ScoreBreakdown]] = []
        matches: list[tuple[_Candidate, str, _ScoreBreakdown]] = []
        for candidate in pool:
            match = pattern.search(candidate.label)
            if match is None:
                continue
            raw_key = generator.row_key(match).strip()
            if not raw_key:
                continue
            breakdown = _row_breakdown(field, candidate)
            pattern_matched.append((candidate, raw_key, breakdown))
            if breakdown.final < field.locate.minimum_confidence:
                continue
            if shape_pattern is not None and shape_pattern.search(candidate.value) is None:
                continue
            matches.append((candidate, raw_key, breakdown))
        # One group per matched label location (page, table, row): every
        # member shares the printed row label, one candidate per surviving
        # value column. Text candidates are single-column, so a text group
        # never has a rival.
        by_location: dict[tuple[int, int, int], list[tuple[_Candidate, str, _ScoreBreakdown]]] = {}
        for entry in matches:
            candidate = entry[0]
            by_location.setdefault(
                (candidate.page, candidate.table_index, candidate.row), []
            ).append(entry)
        selected: list[
            tuple[_Candidate, str, _ScoreBreakdown, tuple[_Candidate, _ScoreBreakdown] | None]
        ] = []
        for entries in by_location.values():
            ranked = sorted(entries, key=lambda item: item[2].final, reverse=True)
            best = ranked[0]
            rival = None
            if len(ranked) > 1 and ranked[1][2].final >= best[2].final - self.ambiguity_gap:
                # A near tie between the label's two best columns is only a
                # real tie when the rival carries the requested column context
                # as incompletely as the winner; near-identical headers
                # ("Check I [4,000 iterations]" against "Check II [5,000
                # iterations]") sit inside the gap on fuzzy similarity alone.
                # The tie resolves toward whichever column carries the
                # requested column_labels completely — equal scores keep
                # column order, so that column can be the ranked-second one.
                best_complete = _is_complete_column_match(field, best[0])
                second_complete = _is_complete_column_match(field, ranked[1][0])
                if best_complete == second_complete:
                    rival = (ranked[1][0], ranked[1][2])
                elif second_complete:
                    best = ranked[1]
            selected.append((best[0], best[1], best[2], rival))
        notes: list[FieldExtraction] = []
        if len(selected) > generator.max_rows:
            dropped = len(selected) - generator.max_rows
            selected = selected[: generator.max_rows]
            notes.append(
                self._row_note(
                    field,
                    f"rows.max_rows caps generation at {generator.max_rows}; {dropped} later "
                    "matching row(s) were not generated or compared (ROW_LIMIT_EXCEEDED).",
                )
            )
        if not selected:
            if not pattern_matched:
                return [
                    self._row_note(
                        field,
                        f"No candidate label matched rows.label_pattern {generator.label_pattern!r}.",
                    )
                ]
            # Labels matched but every candidate fell to a selection filter:
            # the note shows the best survivor's score against the floor (and
            # the value shape it broke, when it broke one) instead of claiming
            # no label matched.
            best_candidate, _, best_breakdown = max(pattern_matched, key=lambda item: item[2].final)
            broke_shape = shape_pattern is not None and (
                shape_pattern.search(best_candidate.value) is None
            )
            return [
                self._row_note(
                    field,
                    f"Rows matched rows.label_pattern {generator.label_pattern!r} but no "
                    f"candidate survived selection; best {best_breakdown.sentence(field.locate.minimum_confidence)}"
                    + (
                        f", and its value {best_candidate.value!r} does not match "
                        f"locate.value_pattern {shape!r}."
                        if broke_shape
                        else "."
                    ),
                )
            ]
        seen: dict[str, int] = {}
        rows: list[FieldExtraction] = []
        for candidate, raw_key, breakdown, rival in selected:
            base = _sanitize_row_key(raw_key)
            occurrence = seen.get(base, 0) + 1
            seen[base] = occurrence
            row_key = base if occurrence == 1 else f"{base}_{occurrence}"
            row_field_key = f"{field.key}.{row_key}"
            collision = (
                f" Row key {base!r} already used by an earlier match; suffixed {row_key!r}."
                if occurrence > 1
                else ""
            )
            row_id = field_identifier(specification_id, field.section_key, field.key, row_key)
            if rival is not None:
                # The label's value columns could not be told apart: loud, in
                # the row's own comparison, instead of one guessed column.
                # The rival is named by its column header and where that
                # column sits — the same naming a static field's tie gets.
                rival_candidate, rival_breakdown = rival
                rows.append(
                    FieldExtraction(
                        field_id=row_id,
                        field_key=row_field_key,
                        section_key=field.section_key,
                        status=ExtractionStatus.AMBIGUOUS,
                        confidence=breakdown.final,
                        explanation=(
                            f"Row label matched, but value columns {_column_name(candidate)!r} "
                            f"({breakdown.final:.2f}) and {_column_name(rival_candidate)!r} "
                            f"({rival_breakdown.final:.2f}, page {rival_candidate.page}, "
                            f"column {rival_candidate.column}) tie inside the "
                            f"{self.ambiguity_gap:.2f} ambiguity gap; locate.column_labels must "
                            f"separate them ({breakdown.sentence(field.locate.minimum_confidence)})"
                            f".{collision}"
                        ),
                        column_context=candidate.column_context,
                        row_context=candidate.row_context,
                        row_key=row_key,
                        row_generator_id=field.id,
                        **self._score_fields(
                            breakdown,
                            field,
                            _Rival(
                                rival_breakdown,
                                rival_candidate,
                                _column_name(rival_candidate),
                            ),
                        ),
                    )
                )
                continue
            # The same value contract a single field honours, per row: a row
            # that matched the label and the value shape still has to parse.
            component, component_error = _extract_component(field, candidate.value)
            if component is None:
                rows.append(
                    FieldExtraction(
                        field_id=row_id,
                        field_key=row_field_key,
                        section_key=field.section_key,
                        status=ExtractionStatus.NOT_EXTRACTED,
                        raw_document_value=candidate.value,
                        confidence=breakdown.final,
                        explanation=(
                            f"{component_error or 'The document value could not be parsed.'} "
                            f"{breakdown.sentence(field.locate.minimum_confidence)}.{collision}"
                        ),
                        column_context=candidate.column_context,
                        row_context=candidate.row_context,
                        row_key=row_key,
                        row_generator_id=field.id,
                        **self._score_fields(breakdown, field),
                    )
                )
                continue
            rows.append(
                FieldExtraction(
                    field_id=row_id,
                    field_key=row_field_key,
                    section_key=field.section_key,
                    status=ExtractionStatus.EXTRACTED,
                    raw_document_value=candidate.value,
                    normalized_document_value=component,
                    location=EvidenceLocation(
                        page=candidate.page,
                        table_index=candidate.table_index,
                        row=candidate.row,
                        column=candidate.column,
                        raw_text=candidate.value,
                        cell_bbox=candidate.cell_bbox,
                        table_bbox=candidate.table_bbox,
                    ),
                    confidence=breakdown.final,
                    explanation=(
                        f"Generated row {row_field_key!r} selected "
                        f"{_LOCATION_NOUN[field.locate.strategy]} using {field.locate.strategy} "
                        f"with confidence {breakdown.final:.3f} "
                        f"({breakdown.sentence(field.locate.minimum_confidence)}).{collision}"
                    ),
                    column_context=candidate.column_context,
                    row_context=candidate.row_context,
                    row_key=row_key,
                    row_generator_id=field.id,
                    **self._score_fields(breakdown, field),
                )
            )
        return rows + notes

    @staticmethod
    def _score_fields(
        breakdown: _ScoreBreakdown, field: PlannedField, rival: _Rival | None = None
    ) -> Any:
        """The extraction artifact's optional score-breakdown fields for one
        winning candidate (and, on contested outcomes, its runner-up — score,
        identifying text, and location), as a kwargs bundle for
        FieldExtraction."""
        return {
            "label_score": breakdown.label,
            "section_factor": breakdown.section_factor,
            "column_factor": breakdown.column_factor,
            "final_score": breakdown.final,
            "minimum_confidence": field.locate.minimum_confidence,
            "runner_up_score": rival.breakdown.final if rival is not None else None,
            **(_rival_fields(rival) if rival is not None else {}),
        }

    @staticmethod
    def _row_note(field: PlannedField, message: str) -> FieldExtraction:
        """A generator-level extraction entry: no row was generated (nothing
        matched) or the cap fired. It carries the generator's own id and no
        row key, so the comparison surfaces it as one loud row."""
        return FieldExtraction(
            field_id=field.id,
            field_key=field.key,
            section_key=field.section_key,
            status=ExtractionStatus.NOT_EXTRACTED,
            explanation=message,
            row_generator_id=field.id,
        )

    def _extract_text_span(
        self,
        field: PlannedField,
        locator: TextSpanLocator,
        lines: list[_SpanLine],
        zones: list[_TableZone],
    ) -> FieldExtraction:
        """Locate one text_span field: score anchor lines, resolve ties the
        way text_label does, and concatenate the prose that follows the
        winner — with detected-table lines kept out of that prose unless
        `exclude_tables` is turned off."""
        ranked = sorted(
            (
                (_detailed_score(field, _anchor_candidate(line)), index, line)
                for index, line in enumerate(lines)
                if len(line.text) <= _SPAN_ANCHOR_MAX_CHARS
            ),
            key=lambda item: item[0].final,
            reverse=True,
        )
        if not ranked or ranked[0][0].final < locator.minimum_confidence:
            best_breakdown = ranked[0][0] if ranked else None
            return FieldExtraction(
                field_id=field.id,
                field_key=field.key,
                section_key=field.section_key,
                status=ExtractionStatus.NOT_EXTRACTED,
                explanation=(
                    "No text span anchor met the configured confidence threshold"
                    + (
                        f"; best {best_breakdown.sentence(locator.minimum_confidence)}."
                        if best_breakdown is not None
                        else "."
                    )
                ),
                **(self._score_fields(best_breakdown, field) if best_breakdown is not None else {}),
            )
        best_breakdown, best_index, best_line = ranked[0]
        best_score = best_breakdown.final
        tied = [item for item in ranked if item[0].final >= best_score - self.ambiguity_gap]
        agreed_tie = False
        if len(tied) > 1:
            # A near tie resolves toward the anchor that carries the requested
            # alias as a COMPLETE token set: a wrapped column header whose
            # tokens ("Computational", "Reproducibility") all sit inside the
            # alias ties at 1.0 on token-set similarity but carries the alias
            # only as a subset. Equal scores keep document order, so the
            # complete carrier can sit BELOW its subset rival in the ranking —
            # promote it when it does.
            best_is_complete = _is_complete_match(field, _anchor_candidate(best_line))
            rival_item = tied[1]
            rival_is_complete = _is_complete_match(field, _anchor_candidate(rival_item[2]))
            if rival_is_complete and not best_is_complete:
                best_breakdown, best_index, best_line = rival_item
                best_score = best_breakdown.final
                rival_item = tied[0]
                best_is_complete, rival_is_complete = True, False
            resolved = best_is_complete and not rival_is_complete
            if not resolved:
                # A heading printed on every page anchors one span per copy.
                # The copies agree only when they define the same span.
                span_texts = {
                    _folded(_span_text(_span_lines(locator, lines, index, zones)[0]))
                    for _, index, _ in tied
                }
                agreed_tie = len(span_texts) == 1
                resolved = agreed_tie
            if not resolved:
                # Name the rival anchor — its text and page — so the tie can
                # be fixed (end_labels, a more specific alias) without
                # reverse-engineering it through the evidence.
                rival = _Rival(rival_item[0], _anchor_candidate(rival_item[2]), rival_item[2].text)
                return FieldExtraction(
                    field_id=field.id,
                    field_key=field.key,
                    section_key=field.section_key,
                    status=ExtractionStatus.AMBIGUOUS,
                    confidence=best_score,
                    explanation=(
                        "Multiple lines have equally strong anchor evidence and define "
                        "different text spans; best "
                        f"{best_breakdown.sentence(locator.minimum_confidence)}, runner-up "
                        f"{rival.clause()} at {rival_item[0].final:.2f} inside the "
                        f"{self.ambiguity_gap:.2f} ambiguity gap."
                    ),
                    **self._score_fields(best_breakdown, field, rival),
                )
        content, end_line, report = _span_lines(locator, lines, best_index, zones)
        if not content:
            return FieldExtraction(
                field_id=field.id,
                field_key=field.key,
                section_key=field.section_key,
                status=ExtractionStatus.NOT_EXTRACTED,
                raw_document_value="",
                confidence=best_score,
                explanation=(
                    f"The matched anchor {best_line.text!r} is followed by no text "
                    f"before the span ends ({best_breakdown.sentence(locator.minimum_confidence)})"
                    + (
                        f" {report.excluded} ruled-table line(s) after it were excluded "
                        "and no prose remains."
                        if locator.exclude_tables and report.excluded
                        else ""
                    )
                    + "."
                ),
                **self._score_fields(best_breakdown, field),
            )
        value = _span_text(content)
        first, last = content[0], content[-1]
        location = EvidenceLocation(
            page=first.page,
            table_index=0,
            row=first.row,
            column=1,
            raw_text=value,
            # A span can cover several pages; a rectangle only means something
            # when it sits on one.
            cell_bbox=_union_bbox(content) if first.page == last.page else None,
            table_bbox=None,
            end_page=last.page,
            end_row=last.row,
        )
        explanation = (
            f"Selected text span after anchor {best_line.text!r} using text_span "
            f"with confidence {best_score:.3f} "
            f"({best_breakdown.sentence(locator.minimum_confidence)})."
        )
        explanation += (
            f" Span ended before {end_line.text!r}."
            if end_line is not None
            else " Span ran to the end of the document."
        )
        if locator.exclude_tables:
            if report.excluded:
                explanation += f" Excluded {report.excluded} ruled-table line(s)."
            if report.unexcludable:
                explanation += (
                    f" {len(report.unexcludable)} table(s) inside the span have no "
                    "reliably ruled borders and kept their lines (exclude_tables "
                    "cannot remove them)."
                )
        if agreed_tie:
            explanation += " Tied anchors defined the same span."
        return FieldExtraction(
            field_id=field.id,
            field_key=field.key,
            section_key=field.section_key,
            status=ExtractionStatus.EXTRACTED,
            raw_document_value=value,
            normalized_document_value=value,
            location=location,
            confidence=best_score,
            explanation=explanation,
            column_context=None,
            row_context=best_line.text,
            **self._score_fields(best_breakdown, field),
        )
