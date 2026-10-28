"""Canonical text from positioned words, shared by every engine.

An engine hands over each page as blocks of lines of words in the page's reading frame (the
unrotated page turned so that most text runs left to right; see `reading_frame`), plus table
grids. The frame follows the text, not /Rotate: a landscape table may be drawn sideways on an
unrotated page, or upright on a page the viewer turns. This module decides reading order, writes the
NFC text with its separators, and records every word, line, cell, and page with offsets and
unrotated boxes. Two engines that read the same characters therefore produce comparable layers.

Separators: words " ", lines "\\n", blocks "\\n\\n", table cells "\\t", table rows "\\n", pages
"\\n\\f\\n". `kittum.core.find_quote` treats all of them as whitespace, so a quote may cross a line,
column, or cell boundary.
"""

import unicodedata
from dataclasses import dataclass, field

from kittum.documents.model import (
    Box,
    Line,
    Page,
    PageTriage,
    RawLayer,
    Table,
    TableCell,
    Word,
)

ASSEMBLY_VERSION = "kittum.documents.layout.v2"

type Rect = tuple[float, float, float, float]

_LIGATURES = str.maketrans(
    {
        "\ufb00": "ff",
        "\ufb01": "fi",
        "\ufb02": "fl",
        "\ufb03": "ffi",
        "\ufb04": "ffl",
        "\ufb05": "st",
        "\ufb06": "st",
    }
)

# Header and footer bands, as fractions of the page height.
_HEADER_BAND = 0.08
_FOOTER_BAND = 0.93
_COLUMN_GAP = 5.0  # points of empty space that separate two columns
_GUTTER_OVERLAP = 3.0


def normalize_word(text: str) -> str:
    """NFC with ligatures expanded and invisible characters dropped.

    A soft hyphen that ends a word is the hyphen printed at a line break, so it becomes "-" and
    `find_quote` can join the word across the break. Elsewhere it is dropped.
    """
    text = unicodedata.normalize("NFC", text.translate(_LIGATURES)).strip()
    hyphen = text.endswith("\u00ad")
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf"))
    return text + "-" if hyphen and text else text


@dataclass(slots=True)
class PWord:
    """A word in reading-frame coordinates."""

    text: str
    rect: Rect

    @property
    def cx(self) -> float:
        return (self.rect[0] + self.rect[2]) / 2

    @property
    def cy(self) -> float:
        return (self.rect[1] + self.rect[3]) / 2


@dataclass(slots=True)
class PBlock:
    lines: list[list[PWord]]
    order: float | None = None  # reading-order rank from a layout model, if any

    @property
    def rect(self) -> Rect:
        return union(w.rect for line in self.lines for w in line)


@dataclass(slots=True)
class GridCell:
    row: int
    col: int
    rect: Rect
    row_span: int = 1
    col_span: int = 1
    header: bool = False
    words: list[PWord] = field(default_factory=list)


@dataclass(slots=True)
class Grid:
    rect: Rect
    rows: int
    cols: int
    cells: list[GridCell]
    order: float | None = None


@dataclass(slots=True)
class PageInput:
    """One page's words in its reading frame: the unrotated page turned clockwise by `frame`
    degrees so that most text runs left to right. `width` and `height` are that frame's size."""

    number: int  # 1-based
    width: float
    height: float
    rotation: int  # the PDF's /Rotate, passed through for the viewer
    blocks: list[PBlock]
    grids: list[Grid] = field(default_factory=list)
    label: str | None = None
    triage: PageTriage | None = None
    frame: int = 0


def reading_frame(directions: list[tuple[float, float, int]]) -> int:
    """The clockwise turn of the unrotated page that makes most text read left to right.

    `directions` are (dx, dy, characters) per line, with dx, dy the baseline direction in the
    unrotated top-left frame.
    """
    votes = {0: 0, 90: 0, 180: 0, 270: 0}
    for dx, dy, n in directions:
        if abs(dx) >= abs(dy):
            votes[0 if dx > 0 else 180] += n
        else:
            votes[90 if dy < 0 else 270] += n
    return max(votes, key=lambda k: (votes[k], k == 0))


def union(rects) -> Rect:
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for r in rects:
        x0, y0 = min(x0, r[0]), min(y0, r[1])
        x1, y1 = max(x1, r[2]), max(y1, r[3])
    return (x0, y0, x1, y1)


def contains(rect: Rect, x: float, y: float, slack: float = 0.0) -> bool:
    return rect[0] - slack <= x <= rect[2] + slack and rect[1] - slack <= y <= rect[3] + slack


def unrotate(rect: Rect, rotation: int, width: float, height: float) -> Rect:
    """Map a rect from a frame turned clockwise by `rotation` back to the unrotated page.

    `width` and `height` are the turned frame's size. The inverse of `rotate`.
    """
    x0, y0, x1, y1 = rect
    match rotation % 360:
        case 90:  # turned (x', y') = (H - y, x), with H the unrotated height = turned width
            pts = [(y0, width - x0), (y1, width - x1)]
        case 180:
            pts = [(width - x0, height - y0), (width - x1, height - y1)]
        case 270:  # turned (x', y') = (y, W - x), with W the unrotated width = turned height
            pts = [(height - y0, x0), (height - y1, x1)]
        case _:
            return rect
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def rotate(rect: Rect, rotation: int, width: float, height: float) -> Rect:
    """Map an unrotated rect (page `width` x `height`) into the frame turned clockwise by
    `rotation`, as PyMuPDF's `Page.rotation_matrix` does."""
    x0, y0, x1, y1 = rect
    match rotation % 360:
        case 90:
            pts = [(height - y0, x0), (height - y1, x1)]
        case 180:
            pts = [(width - x0, height - y0), (width - x1, height - y1)]
        case 270:
            pts = [(y0, width - x0), (y1, width - x1)]
        case _:
            return rect
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def group_rows(words: list[PWord]) -> list[list[PWord]]:
    """Group words into printed lines by vertical overlap, each line sorted left to right."""
    rows: list[tuple[float, float, list[PWord]]] = []
    for w in sorted(words, key=lambda w: (w.cy, w.rect[0])):
        h = max(w.rect[3] - w.rect[1], 0.1)
        if rows:
            top, bottom, members = rows[-1]
            overlap = min(bottom, w.rect[3]) - max(top, w.rect[1])
            if overlap >= 0.5 * min(h, bottom - top):
                rows[-1] = (min(top, w.rect[1]), max(bottom, w.rect[3]), [*members, w])
                continue
        rows.append((w.rect[1], w.rect[3], [w]))
    return [sorted(members, key=lambda w: w.rect[0]) for _, _, members in rows]


def _gaps(intervals: list[tuple[float, float]], lo: float, hi: float) -> list[tuple[float, float]]:
    """Empty stretches of at least `_COLUMN_GAP` inside [lo, hi] not covered by `intervals`."""
    gaps: list[tuple[float, float]] = []
    cursor = lo
    for a, b in sorted(intervals):
        if a - cursor >= _COLUMN_GAP:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
    if hi - cursor >= _COLUMN_GAP:
        gaps.append((cursor, hi))
    return gaps


type Item = PBlock | Grid


def _rect(item: Item) -> Rect:
    return item.rect


def _split(items: list[Item], axis: int) -> list[list[Item]]:
    """Split items at empty bands along an axis (0 = x, 1 = y), in increasing order."""
    lo, hi = (0, 2) if axis == 0 else (1, 3)
    ordered = sorted(items, key=lambda i: _rect(i)[lo])
    groups: list[list[Item]] = []
    reach = float("-inf")
    min_gap = _COLUMN_GAP if axis == 0 else 0.5
    for item in ordered:
        r = _rect(item)
        if groups and r[lo] < reach + min_gap:
            groups[-1].append(item)
            reach = max(reach, r[hi])
        else:
            groups.append([item])
            reach = r[hi]
    return groups


def _band_gutters(band: list[Item], lo: float, hi: float) -> list[tuple[float, float]]:
    return _gaps([(_rect(i)[0], _rect(i)[2]) for i in band], lo, hi)


def _shares_gutter(a: list[Item], b: list[Item], lo: float, hi: float) -> bool:
    for g0, g1 in _band_gutters(a, lo, hi):
        for h0, h1 in _band_gutters(b, lo, hi):
            if min(g1, h1) - max(g0, h0) >= _GUTTER_OVERLAP:
                return True
    return False


def _by_rows(items: list[Item]) -> list[Item]:
    """Top to bottom, and left to right among items that share a printed line."""
    rows: list[tuple[float, list[Item]]] = []
    for item in sorted(items, key=lambda i: _rect(i)[1]):
        r = _rect(item)
        if rows and r[1] < rows[-1][0]:
            bottom, members = rows[-1]
            rows[-1] = (max(bottom, r[3]), [*members, item])
        else:
            rows.append((r[3], [item]))
    return [i for _, members in rows for i in sorted(members, key=lambda i: _rect(i)[0])]


def reading_order(items: list[Item]) -> list[Item]:
    """Order blocks and tables the way a reader reads a multi-column page.

    Horizontal bands are cut at empty rows. Neighbouring bands that share a column gutter belong to
    the same column set and are merged; a band without one (a full-width title, table, or figure)
    stays on its own. Each merged band is read column by column, each column recursively.
    """
    if len(items) <= 1:
        return list(items)
    lo = min(_rect(i)[0] for i in items)
    hi = max(_rect(i)[2] for i in items)
    merged: list[list[Item]] = []
    for band in _split(items, axis=1):
        if merged and _shares_gutter(merged[-1], band, lo, hi):
            merged[-1].extend(band)
        else:
            merged.append(band)
    out: list[Item] = []
    for band in merged:
        columns = _split(band, axis=0)
        if len(columns) > 1:
            for column in columns:
                out.extend(reading_order(column))
        elif len(merged) > 1:
            out.extend(reading_order(band))
        else:
            out.extend(sorted(band, key=lambda i: (_rect(i)[1], _rect(i)[0])))
    return out


def page_order(page: PageInput, items: list[Item]) -> list[Item]:
    """Running headers first, then the body in reading order, then running footers.

    Items a layout model already ranked keep that rank; the rest follow in geometric order.
    """
    ranked = sorted((i for i in items if i.order is not None), key=lambda i: i.order or 0.0)
    rest = [i for i in items if i.order is None]
    header = [i for i in rest if _rect(i)[3] <= page.height * _HEADER_BAND]
    footer = [i for i in rest if _rect(i)[1] >= page.height * _FOOTER_BAND]
    furniture = {id(i) for i in (*header, *footer)}
    body = [i for i in rest if id(i) not in furniture]
    return [*_by_rows(header), *ranked, *reading_order(body), *_by_rows(footer)]


def _assign_to_cells(grid: Grid, words: list[PWord]) -> None:
    for w in words:
        inside = [c for c in grid.cells if contains(c.rect, w.cx, w.cy)]
        if not inside:
            inside = sorted(
                grid.cells,
                key=lambda c: (
                    max(c.rect[0] - w.cx, 0, w.cx - c.rect[2])
                    + max(c.rect[1] - w.cy, 0, w.cy - c.rect[3])
                ),
            )[:1]
        if inside:
            inside[0].words.append(w)


class _Writer:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pos = 0
        self.words: list[Word] = []
        self.lines: list[Line] = []
        self.cells: list[TableCell] = []
        self.tables: list[Table] = []
        self.pages: list[Page] = []

    def put(self, text: str) -> None:
        self.parts.append(text)
        self.pos += len(text)

    def box(self, page: PageInput, rect: Rect) -> Box:
        x0, y0, x1, y1 = unrotate(rect, page.frame, page.width, page.height)
        return Box(
            page=page.number, x0=round(x0, 2), y0=round(y0, 2), x1=round(x1, 2), y1=round(y1, 2)
        )

    def line(self, page: PageInput, words: list[PWord]) -> bool:
        """Write one line of words. Returns False if nothing survived normalization."""
        kept = [(normalize_word(w.text), w) for w in words]
        kept = [(t, w) for t, w in kept if t]
        if not kept:
            return False
        start = self.pos
        for i, (text, w) in enumerate(kept):
            if i:
                self.put(" ")
            self.words.append(
                Word(start=self.pos, end=self.pos + len(text), box=self.box(page, w.rect))
            )
            self.put(text)
        self.lines.append(
            Line(start=start, end=self.pos, box=self.box(page, union(w.rect for _, w in kept)))
        )
        return True

    def mark(self) -> tuple[int, int]:
        return len(self.parts), self.pos

    def rollback(self, mark: tuple[int, int]) -> None:
        del self.parts[mark[0] :]
        self.pos = mark[1]

    def block(self, page: PageInput, block: PBlock) -> bool:
        wrote = False
        for line in block.lines:
            mark = self.mark()
            if wrote:
                self.put("\n")
            if self.line(page, line):
                wrote = True
            else:
                self.rollback(mark)
        return wrote

    def grid(self, page: PageInput, grid: Grid, index: int) -> None:
        table_id = f"p{page.number}t{index}"
        start = self.pos
        for r in range(grid.rows):
            if r:
                self.put("\n")
            row_cells = sorted((c for c in grid.cells if c.row == r), key=lambda c: c.col)
            for i, cell in enumerate(row_cells):
                if i:
                    self.put("\t")
                cell_start = self.pos
                for j, line in enumerate(group_rows(cell.words)):
                    if j and self.pos > cell_start:
                        self.put(" ")
                    self.line(page, line)
                self.cells.append(
                    TableCell(
                        table_id=table_id,
                        row=cell.row,
                        col=cell.col,
                        row_span=cell.row_span,
                        col_span=cell.col_span,
                        start=cell_start,
                        end=self.pos,
                        box=self.box(page, cell.rect),
                        header=cell.header,
                    )
                )
        self.tables.append(
            Table(
                id=table_id,
                page=page.number,
                box=self.box(page, grid.rect),
                rows=grid.rows,
                cols=grid.cols,
                start=start,
                end=self.pos,
            )
        )


def _with_header_paths(cells: list[TableCell], text: str) -> list[TableCell]:
    """Give every cell the texts of the header cells above it that cover its columns."""
    by_table: dict[str, list[TableCell]] = {}
    for cell in cells:
        by_table.setdefault(cell.table_id, []).append(cell)
    out: list[TableCell] = []
    for table_cells in by_table.values():
        headers = sorted((c for c in table_cells if c.header), key=lambda c: (c.row, c.col))
        for cell in table_cells:
            path = tuple(
                " ".join(text[h.start : h.end].split())
                for h in headers
                if h.row < cell.row
                and h.end > h.start
                and h.col <= cell.col
                and cell.col + cell.col_span <= h.col + h.col_span
            )
            out.append(cell.model_copy(update={"header_path": path}) if path else cell)
    return out


def assemble(pages: list[PageInput]) -> RawLayer:
    w = _Writer()
    for n, page in enumerate(pages):
        if n:
            w.put("\n\f\n")
        page_start = w.pos
        blocks = [b for b in page.blocks if any(b.lines)]
        for grid in page.grids:
            taken: list[PWord] = []
            for block in blocks:
                kept_lines = []
                for line in block.lines:
                    inside = [x for x in line if contains(grid.rect, x.cx, x.cy)]
                    taken.extend(inside)
                    rest = [x for x in line if not contains(grid.rect, x.cx, x.cy)]
                    if rest:
                        kept_lines.append(rest)
                block.lines = kept_lines
            _assign_to_cells(grid, taken)
            blocks = [b for b in blocks if b.lines]
        items: list[Item] = [*blocks, *page.grids]
        first = True
        table_index = 0
        for item in page_order(page, items):
            if isinstance(item, Grid):
                if not first:
                    w.put("\n\n")
                w.grid(page, item, table_index)
                table_index += 1
                first = False
            else:
                mark = w.mark()
                if not first:
                    w.put("\n\n")
                if w.block(page, item):
                    first = False
                else:
                    w.rollback(mark)
        w.pages.append(
            Page(
                number=page.number,
                width=round(page.height if page.frame % 180 else page.width, 2),
                height=round(page.width if page.frame % 180 else page.height, 2),
                rotation=page.rotation % 360,
                frame=page.frame % 360,
                label=page.label or None,
                start=page_start,
                end=w.pos,
                triage=page.triage,
            )
        )
    text = "".join(w.parts)
    return RawLayer(
        text=text,
        words=tuple(w.words),
        lines=tuple(w.lines),
        cells=tuple(_with_header_paths(w.cells, text)),
        tables=tuple(w.tables),
        pages=tuple(w.pages),
    )
