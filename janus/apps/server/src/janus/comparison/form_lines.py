"""Rebuilds label/value rows from aligned text spans.

A bordered form that arrives as a raster image with a text overlay defeats
both table detectors: the rule lines are pixels, so the layout detector only
recovers stray fragments, and the statistical text grid shatters the form's
short labels across unrelated prose columns. What such a page does have is
aligned label/value lines — a label span with its value printed to the right
on the same baseline. This module turns those pairs into an ordinary
two-column table so the table-label locator works unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from janus.comparison.content import BoundingBox, TableCell, TableStructure

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pymupdf

# A value is money, a date, an uppercase code with digits ("2023 1234567",
# "123 MAIN STREET"), or a short uppercase token ("RE").
_MONEY = re.compile(r"^[$€£]?[(+]?\d[\d,]*(?:\.\d+)?[)]?$")
_DATE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")
_CODE = re.compile(r"^[A-Z0-9][A-Z0-9 ./&+-]*$")
_SHORT_CODE = re.compile(r"^[A-Z0-9][A-Z0-9./-]{1,5}$")

# A label carries a real word, is short, and does not end like a sentence.
_WORD = re.compile(r"[A-Za-z]{2}")
_PROSE_TAIL = ".,;"

_MAX_LABEL_CHARS = 40
_MAX_LABEL_WORDS = 5
_MAX_VALUE_CHARS = 24
# A form line keeps its value close; a span far to the right belongs to
# another column of the page.
_VALUE_GAP_FACTOR = 0.25
# Kerning can make a label slightly overlap the span that follows it.
_MAX_SPAN_OVERLAP = 1.5
# Fewer aligned pairs than this is noise, not a form.
_MIN_NUMERIC_PAIRS = 4


@dataclass(frozen=True)
class _Span:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float


def _is_label(text: str) -> bool:
    token = text.strip()
    if not token or len(token) > _MAX_LABEL_CHARS or len(token.split()) > _MAX_LABEL_WORDS:
        return False
    if not _WORD.search(token):
        return False
    return token[-1] not in _PROSE_TAIL


def _is_value(text: str) -> bool:
    token = text.strip()
    if not token or len(token) > _MAX_VALUE_CHARS or token[-1] in _PROSE_TAIL:
        return False
    if _MONEY.match(token) or _DATE.match(token) or _SHORT_CODE.match(token):
        return True
    return bool(_CODE.match(token)) and any(character.isdigit() for character in token)


def _spans(page: pymupdf.Page) -> list[_Span]:
    spans: list[_Span] = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                if not text:
                    continue
                x0, y0, x1, y1 = span["bbox"]
                spans.append(_Span(text, x0, y0, x1, y1, float(span["size"])))
    return spans


def _shares_baseline(left: _Span, right: _Span) -> bool:
    # Larger type is set on a lower baseline, so the tolerance grows with the
    # smaller of the two font sizes.
    tolerance = min(4.0, max(1.0, 0.3 * min(left.size, right.size)))
    return abs(left.y0 - right.y0) <= tolerance


def aligned_pairs(page: pymupdf.Page) -> list[tuple[_Span, _Span]]:
    """Every label-like span paired with the nearest value-like span to its
    right on the same baseline."""
    spans = _spans(page)
    max_gap = _VALUE_GAP_FACTOR * page.rect.width
    pairs: list[tuple[_Span, _Span]] = []
    for label in spans:
        if not _is_label(label.text):
            continue
        values = [
            span
            for span in spans
            if span is not label
            and span.x0 >= label.x1 - _MAX_SPAN_OVERLAP
            and span.x0 - label.x1 <= max_gap
            and _shares_baseline(label, span)
            and _is_value(span.text)
        ]
        if values:
            pairs.append((label, min(values, key=lambda candidate: candidate.x0)))
    return pairs


def _cell(span: _Span, row: int, column: int, page_number: int) -> TableCell:
    return TableCell(
        text=span.text,
        row=row,
        column=column,
        bbox=BoundingBox(
            x=max(0.0, span.x0),
            y=max(0.0, span.y0),
            width=max(0.0, span.x1 - span.x0),
            height=max(0.0, span.y1 - span.y0),
            page=page_number,
        ),
    )


def build_form_table(page: pymupdf.Page, page_number: int) -> TableStructure | None:
    """Build one two-column label/value table for a page, or None when the
    page does not look like a form (too few numeric pairs)."""
    pairs = aligned_pairs(page)
    numeric = sum(1 for _, value in pairs if any(c.isdigit() for c in value.text))
    if numeric < _MIN_NUMERIC_PAIRS:
        return None
    cells = [
        _cell(span, row, column, page_number)
        for row, pair in enumerate(pairs)
        for column, span in enumerate(pair)
    ]
    return TableStructure(rows=len(pairs), columns=2, cells=cells, header_row=False)


def usable_label_pairs(tables: Iterable[TableStructure]) -> int:
    """Count the rows a table-label locator could use: a label-like first cell
    with a non-empty cell to its right."""
    usable = 0
    for table in tables:
        cells = {(cell.row, cell.column): cell for cell in table.cells}
        columns = sorted({cell.column for cell in table.cells})
        for row in sorted({cell.row for cell in table.cells}):
            label = cells.get((row, 0))
            if label is None or not _WORD.search(label.text):
                continue
            if any(
                (row, column) in cells and cells[(row, column)].text.strip()
                for column in columns
                if column
            ):
                usable += 1
    return usable
