"""Word-geometry fallback for borderless tables that fragment the detectors.

Rotated, borderless statistical releases defeat both PyMuPDF detectors: the
layout detector finds no rules to follow, and the text strategy misreads the
spacing inside row labels as column gaps, tearing every label into
micro-columns ("Total percen" | "t change (an" | "nual rate)2"). This module
detects that tearing — detector cells whose tokens are not real words from
the page — and rebuilds the table from `page.get_text("words")` geometry
instead: words are grouped into visual lines (rotation aware), numeric value
words are clustered into column bands, the token row above the first data
line becomes the header, and label indentation supplies section context for
the rows nested underneath.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from janus.comparison.content import TableCell, TableStructure

if TYPE_CHECKING:
    import pymupdf

# A detector cell is trustworthy when every whitespace-separated token of its
# text is a word the page actually contains. Below these ratios of
# trustworthy cells the detector output is treated as torn words and rebuilt
# here: the label column must be nearly perfect (a locator reads it), value
# cells tolerate the odd oddity among many intact ones.
LABEL_TRUSTWORTHY_RATIO = 0.9
TRUSTWORTHY_CELL_RATIO = 0.75

_VALUE_TOKEN = re.compile(r"^\$?-?\(?\d[\d.,]*\)?%?$")
_PLACEHOLDER_TOKENS = frozenset({"n.a.", "n/a", "...", "…"})

# Header tokens are short alphanumeric period labels ("2021", "2025r", "Q3",
# "Junp"); values carry decimal points, commas, or currency markers.
_HEADER_TOKEN = re.compile(r"^[A-Za-z0-9]{1,6}$")
_PUNCTUATED_VALUE = re.compile(r"[.,]")

# A line is a data row when its value words cover at least this share of the
# column bands; prose lines quote a few numbers but never fill the grid.
_DATA_COVERAGE = 0.6

# Two line leads closer than this are siblings, however fine the indent grid.
_LEAD_EPSILON = 2.0


def _is_value_token(text: str) -> bool:
    return text in _PLACEHOLDER_TOKENS or bool(_VALUE_TOKEN.match(text))


@dataclass(frozen=True)
class _Word:
    """A page word projected onto reading-order coordinates.

    ``start``/``end`` are the word's extent along the reading direction
    (increasing start = later in reading order); ``line_low``/``line_high``
    span the perpendicular axis, so words on the same visual line overlap
    there.
    """

    start: float
    end: float
    line_low: float
    line_high: float
    text: str


@dataclass
class _Line:
    words: list[_Word] = field(default_factory=list)
    line_low: float = 0.0
    line_high: float = 0.0

    @property
    def lead(self) -> float:
        """Reading position where the line's first word begins.

        Deeper-indented rows start later, so a smaller lead means a row
        further out to the reading edge — a group header candidate.
        """
        return min(word.start for word in self.words)


def _band_for(word: _Word, bands: list[tuple[float, float]]) -> int | None:
    """Index of the band the word overlaps most, or None when it spans gaps."""
    best_index: int | None = None
    best_overlap = 0.0
    for index, (low, high) in enumerate(bands):
        overlap = min(word.end, high) - max(word.start, low)
        if overlap > best_overlap:
            best_overlap = overlap
            best_index = index
    return best_index


def _split_line(line: _Line, bands: list[tuple[float, float]]) -> tuple[str, dict[int, str]]:
    """Split a line into label text and per-band value cells.

    Value-looking words overlapping a band become that band's cell text; the
    remaining words (in reading order) form the label. Prose that happens to
    cross the column grid stays in the label because its words are not value
    tokens.
    """
    cells: dict[int, list[str]] = {}
    label_words: list[str] = []
    for word in line.words:
        column = _band_for(word, bands)
        if column is not None and _is_value_token(word.text):
            cells.setdefault(column, []).append(word.text)
        else:
            label_words.append(word.text)
    return " ".join(label_words), {index: " ".join(parts) for index, parts in cells.items()}


def _reading_projection(page: pymupdf.Page) -> tuple[float, float]:
    """Vector (fx, fy) so that reading position = fx * x + fy * y.

    Text drawn along (1, 0) reads left to right; along (0, -1) — a page
    rotated a quarter turn — reading order descends in y.
    """
    directions: dict[tuple[float, float], int] = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            dir_x, dir_y = line["dir"]
            key = (round(dir_x), round(dir_y))
            directions[key] = directions.get(key, 0) + 1
    dir_x, dir_y = (
        max(directions, key=lambda direction: directions[direction]) if directions else (1.0, 0.0)
    )
    if dir_x > 0:
        return 1.0, 0.0
    if dir_x < 0:
        return -1.0, 0.0
    return 0.0, (1.0 if dir_y > 0 else -1.0)


def _project_words(page: pymupdf.Page) -> list[_Word]:
    fx, fy = _reading_projection(page)
    words: list[_Word] = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        projections = (fx * x0 + fy * y0, fx * x1 + fy * y0, fx * x0 + fy * y1, fx * x1 + fy * y1)
        words.append(
            _Word(
                start=min(projections),
                end=max(projections),
                line_low=x0 if fy else y0,
                line_high=x1 if fy else y1,
                text=str(text).strip(),
            )
        )
    return [word for word in words if word.text]


def _group_lines(words: list[_Word]) -> list[_Line]:
    """Cluster words into visual lines along the perpendicular axis."""

    def joins(line: _Line, word: _Word) -> bool:
        overlap = min(line.line_high, word.line_high) - max(line.line_low, word.line_low)
        smaller = min(line.line_high - line.line_low, word.line_high - word.line_low)
        return smaller <= 0 or overlap > 0.5 * smaller

    lines: list[_Line] = []
    for word in sorted(words, key=lambda item: item.line_low):
        for line in lines:
            if joins(line, word):
                line.words.append(word)
                line.line_low = min(line.line_low, word.line_low)
                line.line_high = max(line.line_high, word.line_high)
                break
        else:
            lines.append(_Line(words=[word], line_low=word.line_low, line_high=word.line_high))
    for line in lines:
        line.words.sort(key=lambda word: word.start)
    lines.sort(key=lambda line: line.line_low)
    return lines


def _column_bands(lines: list[_Line]) -> list[tuple[float, float]]:
    """Merge value-word extents into reading-ordered column bands."""
    intervals: list[tuple[float, float]] = []
    for line in lines:
        intervals.extend(
            (word.start, word.end) for word in line.words if _is_value_token(word.text)
        )
    if not intervals:
        return []
    intervals.sort()
    bands: list[tuple[float, float]] = []
    for start, end in intervals:
        if bands and start <= bands[-1][1]:
            bands[-1] = (bands[-1][0], max(bands[-1][1], end))
        else:
            bands.append((start, end))
    # Column bands recur in every data row; numbers inside prose do not.
    return [band for band in bands if _support(band, intervals) >= 3]


def _support(band: tuple[float, float], intervals: list[tuple[float, float]]) -> int:
    low, high = band
    return sum(1 for start, end in intervals if min(end, high) - max(start, low) > 0)


def _looks_like_header(label: str, values: dict[int, str]) -> bool:
    """Whether a grid-filling line is a header row of period tokens.

    Header rows fail the data-line rules only narrowly when their period
    tokens are numeric ("2022"); their label is then a run of short
    alphanumeric tokens and every value a bare integer, which no measurement
    row of a statistical release is ("3.4" or "n.a." carry punctuation).
    """
    label_tokens = label.split()
    return (
        bool(label_tokens)
        and all(_HEADER_TOKEN.match(token) for token in label_tokens)
        and all(not _PUNCTUATED_VALUE.search(value) for value in values.values())
    )


def _is_data_line(line: _Line, bands: list[tuple[float, float]]) -> bool:
    """True when the line is a table data row: a label plus a filled grid."""
    if not bands:
        return False
    label, values = _split_line(line, bands)
    if not label or not values:
        return False
    if _looks_like_header(label, values):
        return False
    return len(values) >= max(2, int(len(bands) * _DATA_COVERAGE))


def _header_run(
    lines: list[_Line], first_data: int, bands: list[tuple[float, float]]
) -> list[_Line]:
    """Contiguous lines above the first data row that are pure header tokens."""
    run: list[_Line] = []
    for line in reversed(lines[:first_data]):
        aligned = [
            word
            for word in line.words
            if _HEADER_TOKEN.match(word.text) and _band_for(word, bands) is not None
        ]
        if len(aligned) < 2 or len(aligned) < 0.6 * len(line.words):
            break
        run.append(line)
    run.reverse()
    return run


def _section_rows(body: list[tuple[str, dict[int, str], float]]) -> list[str | None]:
    """Section context for every body row from the label indent hierarchy.

    A preceding row heads the current one when its label begins further out
    on the reading edge (a shallower indent, which pure group-label rows and
    summary rows with children both have); the nearest such row is the
    section, tracked as a stack.
    """
    sections: list[str | None] = []
    stack: list[tuple[float, str]] = []
    for _label, _values, lead in body:
        while stack and stack[-1][0] >= lead - _LEAD_EPSILON:
            stack.pop()
        sections.append(stack[-1][1] if stack else None)
        stack.append((lead, _label))
    return sections


def _data_grid(lines: list[_Line]) -> tuple[list[int], list[tuple[float, float]]] | None:
    """Locate data rows and the column bands they share, in two passes.

    Provisional bands come from every value word on the page, which suffices
    to spot data rows; the bands are then recomputed from those rows alone so
    header span tokens ("2025" over quarterly columns) and stray numbers in
    prose can neither bridge adjacent columns nor add empty ones.
    """
    bands = _column_bands(lines)
    if len(bands) < 3:
        return None
    for _ in range(2):
        data_indices = [index for index, line in enumerate(lines) if _is_data_line(line, bands)]
        if len(data_indices) < 2:
            return None
        refined = _column_bands([lines[index] for index in data_indices])
        if len(refined) < 3:
            return None
        if refined == bands:
            return data_indices, bands
        bands = refined
    data_indices = [index for index, line in enumerate(lines) if _is_data_line(line, bands)]
    if len(data_indices) < 2:
        return None
    return data_indices, bands


def rebuild_page_table(page: pymupdf.Page) -> TableStructure | None:
    """Rebuild the page's borderless table from word geometry, or None.

    A result is returned only for a genuine numeric grid: at least three
    column bands, two data rows, and a header token row above them.
    """
    lines = _group_lines(_project_words(page))
    if not lines:
        return None
    grid = _data_grid(lines)
    if grid is None:
        return None
    data_indices, bands = grid
    header = _header_run(lines, data_indices[0], bands)
    if not header:
        return None
    header_end = lines.index(header[-1])
    headers = [""]
    for index in range(len(bands)):
        headers.append(
            " ".join(
                word.text
                for line in header
                for word in line.words
                if _band_for(word, bands) == index
            )
        )
    body: list[tuple[str, dict[int, str], float]] = []
    for line in lines[header_end + 1 :]:
        label, values = _split_line(line, bands)
        if label or values:
            body.append((label, values, line.lead))
    if len(body) < 2:
        return None
    sections = _section_rows(body)

    cells: list[TableCell] = []
    for column, text in enumerate(headers):
        cells.append(TableCell(text=text, row=0, column=column, confidence=1))
    for row_index, ((label, values, _lead), section) in enumerate(zip(body, sections, strict=True)):
        cells.append(
            TableCell(text=label, row=row_index + 1, column=0, confidence=1, section=section)
        )
        for column in range(len(bands)):
            cells.append(
                TableCell(
                    text=values.get(column, ""),
                    row=row_index + 1,
                    column=column + 1,
                    confidence=1,
                    section=section,
                )
            )
    return TableStructure(rows=len(body) + 1, columns=len(bands) + 1, cells=cells, header_row=True)


def _cell_ratios(matrices: list[list[list[str | None]]], words: set[str]) -> tuple[float, float]:
    """Trustworthy-cell ratios for the label column and for the rest.

    A cell is trustworthy when every whitespace-separated token of its text
    is a word the page actually contains; detector cells that tear words
    apart ("al consumpti", "4,85") are not. Torn labels and torn values are
    judged apart: a locator reads the label column, so it must be nearly
    perfect, while a stray odd value cell among many intact ones is no
    evidence of a fragmented grid.
    """
    label_total = label_valid = value_total = value_valid = 0
    for matrix in matrices:
        for row in matrix:
            for column, cell in enumerate(row):
                text = "" if cell is None else str(cell)
                if not text.strip():
                    continue
                trustworthy = all(token.strip() in words for token in text.split())
                if column == 0:
                    label_total += 1
                    label_valid += trustworthy
                else:
                    value_total += 1
                    value_valid += trustworthy
    label_ratio = label_valid / label_total if label_total else 1.0
    value_ratio = value_valid / value_total if value_total else 1.0
    return label_ratio, value_ratio


def rebuild_fragmented_tables(
    found_tables: list, page: pymupdf.Page
) -> list[TableStructure] | None:
    """Replace torn detector output with a word-geometry rebuild.

    Takes the detector's tables for one page and returns rebuilt structures
    when their cells tear words apart and a numeric grid underlies the page;
    returns None otherwise so the caller keeps the detector's output.
    """
    if not found_tables:
        return None
    matrices: list[list[list[str | None]]] = []
    for found in found_tables:
        try:
            matrices.append(found.extract())
        except (AttributeError, ValueError):
            return None
    words = {word[4].strip() for word in page.get_text("words")}
    label_ratio, value_ratio = _cell_ratios(matrices, words)
    if label_ratio >= LABEL_TRUSTWORTHY_RATIO and value_ratio >= TRUSTWORTHY_CELL_RATIO:
        return None
    rebuilt = rebuild_page_table(page)
    return [rebuilt] if rebuilt is not None else None
