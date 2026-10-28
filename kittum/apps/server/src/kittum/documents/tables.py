"""Table grids from the text layer.

PyMuPDF's `find_tables` gates borderless tables with its Layout model, which Kittum does not use
until its source is published (docs/document-tools.md, "PyMuPDF Layout"). Without it, only fully
ruled tables are found. Journal tables are mostly three-rule tables, so this module also finds a
table from its "Table N" caption and the full-width rules below it, then builds the grid from the
words themselves:

- rows are printed lines;
- columns are the x-ranges that most rows leave empty;
- a header phrase that crosses a column gap spans those columns, and a rule printed under a header
  cell extends it over the empty columns it covers ("Primary analysis set" over three arms);
- an empty header cell under a header cell merges into it ("Parameter" over two header rows).

Docling's TableFormer grids go through `grid_from_cells`, so both paths give cell rectangles that
tile the table and receive the text layer's words.
"""

import re
from dataclasses import dataclass
from statistics import median

from kittum.documents.layout import Grid, GridCell, PBlock, PWord, Rect, group_rows, union

TABLES_VERSION = "kittum.documents.tables.v1"

_CAPTION = re.compile(r"^Table\s+\d+[A-Za-z]?\b")
_RULE_TOLERANCE = 12.0  # points by which a table's rules may differ in extent
_CAPTION_TO_RULE = 40.0
_MAX_HEADER_ROWS = 4


@dataclass(frozen=True, slots=True)
class Rule:
    x0: float
    x1: float
    y: float


def horizontal_rules(segments: list[Rect]) -> list[Rule]:
    """Horizontal strokes and hairline rectangles, with touching segments joined.

    `segments` are stroked lines (as degenerate rectangles) and filled rectangles.
    """
    raw = [
        Rule(r[0], r[2], (r[1] + r[3]) / 2)
        for r in segments
        if r[3] - r[1] < 1.5 and r[2] - r[0] > 2
    ]
    raw.sort(key=lambda r: (round(r.y, 1), r.x0))
    joined: list[Rule] = []
    for r in raw:
        last = joined[-1] if joined else None
        if last and abs(last.y - r.y) < 0.5 and r.x0 <= last.x1 + 1.5:
            joined[-1] = Rule(last.x0, max(last.x1, r.x1), last.y)
        else:
            joined.append(r)
    return joined


def caption_regions(blocks: list[PBlock], rules: list[Rule]) -> list[Rect]:
    """Regions of three-rule tables: from the first rule under a "Table N" caption to the last
    rule of the same width before the next caption."""
    captions = []
    for block in blocks:
        if block.lines and block.lines[0]:
            first = " ".join(w.text for w in block.lines[0][:3])
            if _CAPTION.match(first):
                captions.append(block.lines[0][0].rect[1])
    captions.sort()
    regions: list[Rect] = []
    for i, top in enumerate(captions):
        stop = captions[i + 1] if i + 1 < len(captions) else float("inf")
        below = [r for r in rules if top < r.y < min(stop, top + _CAPTION_TO_RULE)]
        if not below:
            continue
        first = min(below, key=lambda r: r.y)
        same = [
            r
            for r in rules
            if first.y < r.y < stop
            and abs(r.x0 - first.x0) <= _RULE_TOLERANCE
            and abs(r.x1 - first.x1) <= _RULE_TOLERANCE
        ]
        if same:
            bottom = max(r.y for r in same)
            regions.append((first.x0, first.y, first.x1, bottom))
    return regions


def _phrases(row: list[PWord], gap: float) -> list[tuple[float, float, list[PWord]]]:
    out: list[tuple[float, float, list[PWord]]] = []
    for w in row:
        if out and w.rect[0] - out[-1][1] < gap:
            x0, x1, ws = out[-1]
            out[-1] = (x0, max(x1, w.rect[2]), [*ws, w])
        else:
            out.append((w.rect[0], w.rect[2], [w]))
    return out


def _columns(
    rows: list[list[tuple[float, float, list[PWord]]]], lo: float, hi: float
) -> list[float]:
    """Column boundaries: x-ranges that at most a tenth of the rows cover."""
    step = 0.5
    n = max(int((hi - lo) / step) + 1, 1)
    cover = [0] * n
    for phrases in rows:
        hit = [False] * n
        for x0, x1, _ in phrases:
            for k in range(max(int((x0 - lo) / step), 0), min(int((x1 - lo) / step) + 1, n)):
                hit[k] = True
        for k in range(n):
            cover[k] += hit[k]
    threshold = max(1, len(rows) // 10)
    bounds = [lo]
    k = 0
    while k < n:
        if cover[k] <= threshold:
            j = k
            while j < n and cover[j] <= threshold:
                j += 1
            if (j - k) * step >= 3.0 and k > 0 and j < n:
                bounds.append(lo + (k + j) / 2 * step)
            k = j
        else:
            k += 1
    bounds.append(hi)
    return bounds


def grid_from_words(rect: Rect, words: list[PWord], rules: list[Rule]) -> Grid | None:
    """Build a table grid from the words inside `rect`."""
    lines = group_rows(words)
    if len(lines) < 2:
        return None
    height = median(w.rect[3] - w.rect[1] for w in words)
    phrase_rows = [_phrases(line, gap=0.45 * height) for line in lines]
    bounds = _columns(phrase_rows, rect[0], rect[2])
    ncols = len(bounds) - 1
    if ncols < 2:
        return None
    row_tops = [min(w.rect[1] for w in line) for line in lines]
    row_bottoms = [max(w.rect[3] for w in line) for line in lines]
    ybounds = [rect[1]]
    ybounds += [(row_bottoms[i] + row_tops[i + 1]) / 2 for i in range(len(lines) - 1)]
    ybounds.append(max(rect[3], row_bottoms[-1]))

    def col_of(x: float) -> int:
        for c in range(ncols):
            if x < bounds[c + 1]:
                return c
        return ncols - 1

    # Each phrase covers the columns it overlaps; overlapping coverage merges into one cell.
    spans_by_row: list[list[tuple[int, int, list[PWord]]]] = []
    for phrases in phrase_rows:
        spans: list[tuple[int, int, list[PWord]]] = []
        for x0, x1, ws in phrases:
            c0, c1 = col_of(x0 + 0.5), col_of(x1 - 0.5)
            if spans and c0 <= spans[-1][1]:
                a, _, prev = spans[-1]
                spans[-1] = (a, max(c1, spans[-1][1]), [*prev, *ws])
            else:
                spans.append((c0, c1, list(ws)))
        spans_by_row.append(spans)

    header_rows = 1
    while (
        header_rows < min(_MAX_HEADER_ROWS, len(lines) - 1)
        and spans_by_row[header_rows]
        and spans_by_row[header_rows][0][0] > 0
    ):
        header_rows += 1

    # Content extent per column, from body rows only.
    extent: list[tuple[float, float] | None] = [None] * ncols
    for spans in spans_by_row[header_rows:]:
        for c0, c1, ws in spans:
            if c0 == c1:
                r = union(w.rect for w in ws)
                e = extent[c0]
                extent[c0] = (r[0], r[2]) if e is None else (min(e[0], r[0]), max(e[1], r[2]))

    # Extend header cells over the empty columns covered by the rule printed under them.
    for r in range(header_rows):
        spans = spans_by_row[r]
        bottom = row_bottoms[r]
        # A rule across the whole table separates header from body; it spans nothing.
        under = [
            x
            for x in rules
            if bottom - 1.5 <= x.y <= ybounds[r + 1] + 3.0
            and x.x1 - x.x0 < 0.9 * (rect[2] - rect[0])
        ]
        for i, (c0, c1, ws) in enumerate(spans):
            left = min(w.rect[0] for w in ws)
            rule = next((x for x in under if x.x0 - 2 <= left <= x.x1), None)
            if rule is None:
                continue
            limit = spans[i + 1][0] if i + 1 < len(spans) else ncols
            end = c1
            for c in range(c1 + 1, limit):
                e = extent[c]
                if e is None or min(rule.x1, e[1]) - max(rule.x0, e[0]) < 0.5 * (e[1] - e[0]):
                    break
                end = c
            spans[i] = (c0, end, ws)

    cells: list[GridCell] = []
    covered: set[tuple[int, int]] = set()
    by_origin: dict[tuple[int, int], GridCell] = {}
    for r, spans in enumerate(spans_by_row):
        filled = {c: (c0, c1, ws) for c0, c1, ws in spans for c in range(c0, c1 + 1)}
        c = 0
        while c < ncols:
            if (r, c) in covered:
                c += 1
                continue
            if c in filled:
                c0, c1, ws = filled[c]
                cell = GridCell(
                    row=r, col=c0, rect=(0, 0, 0, 0), col_span=c1 - c0 + 1, words=list(ws)
                )
                c = c1 + 1
            else:
                above = by_origin.get((r - 1, c)) if 0 < r < header_rows else None
                if above is not None and above.words and above.col_span == 1:
                    above.row_span += 1
                    covered.add((r, c))
                    c += 1
                    continue
                cell = GridCell(row=r, col=c, rect=(0, 0, 0, 0))
                c += 1
            cell.header = r < header_rows
            by_origin[(r, cell.col)] = cell
            cells.append(cell)
    for cell in cells:
        cell.rect = (
            bounds[cell.col],
            ybounds[cell.row],
            bounds[cell.col + cell.col_span],
            ybounds[cell.row + cell.row_span],
        )
        cell.words = []  # `layout.assemble` assigns the words again, by position
    return Grid(
        rect=(rect[0], rect[1], rect[2], ybounds[-1]), rows=len(lines), cols=ncols, cells=cells
    )


def _boundaries(
    n: int, extents: dict[int, tuple[float, float]], lo: float, hi: float
) -> list[float]:
    """Boundaries between n slots, midway between neighbouring slots' extents."""
    known = sorted(extents)
    if not known:
        return [lo + (hi - lo) * i / n for i in range(n + 1)]
    bounds = [lo]
    for i in range(1, n):
        left = max((k for k in known if k < i), default=None)
        right = min((k for k in known if k >= i), default=None)
        if left is not None and right is not None:
            a, b = extents[left][1], extents[right][0]
            share = (i - left) / (right - left + 1) if right - left > 1 else 0.5
            bounds.append(a + (b - a) * share if b > a else (a + b) / 2)
        elif left is not None:
            a = extents[left][1]
            bounds.append(a + (hi - a) * (i - left) / (n - left))
        else:
            assert right is not None
            b = extents[right][0]
            bounds.append(lo + (b - lo) * i / max(right, 1))
    bounds.append(hi)
    for i in range(1, len(bounds)):
        bounds[i] = max(bounds[i], bounds[i - 1])
    return bounds


@dataclass(frozen=True, slots=True)
class CellSpec:
    """A detector's cell: grid position, spans, a text box, and whether it is a column header."""

    row: int
    col: int
    row_span: int
    col_span: int
    rect: Rect
    header: bool


def grid_from_cells(rect: Rect, rows: int, cols: int, specs: list[CellSpec]) -> Grid:
    """A grid whose cells tile `rect`, from a detector's cells (TableFormer gives text boxes,
    not cell boxes, and none for empty cells)."""
    col_ext: dict[int, tuple[float, float]] = {}
    row_ext: dict[int, tuple[float, float]] = {}
    for s in specs:
        if s.rect[2] <= s.rect[0]:
            continue
        if s.col_span == 1:
            e = col_ext.get(s.col)
            col_ext[s.col] = (
                (s.rect[0], s.rect[2])
                if e is None
                else (min(e[0], s.rect[0]), max(e[1], s.rect[2]))
            )
        if s.row_span == 1:
            e = row_ext.get(s.row)
            row_ext[s.row] = (
                (s.rect[1], s.rect[3])
                if e is None
                else (min(e[0], s.rect[1]), max(e[1], s.rect[3]))
            )
    xb = _boundaries(cols, col_ext, rect[0], rect[2])
    yb = _boundaries(rows, row_ext, rect[1], rect[3])
    cells: list[GridCell] = []
    taken: set[tuple[int, int]] = set()
    for s in sorted(specs, key=lambda s: (s.row, s.col)):
        if (s.row, s.col) in taken:
            continue
        rs = max(1, min(s.row_span, rows - s.row))
        cs = max(1, min(s.col_span, cols - s.col))
        for r in range(s.row, s.row + rs):
            for c in range(s.col, s.col + cs):
                taken.add((r, c))
        cells.append(
            GridCell(
                row=s.row,
                col=s.col,
                rect=(xb[s.col], yb[s.row], xb[s.col + cs], yb[s.row + rs]),
                row_span=rs,
                col_span=cs,
                header=s.header,
            )
        )
    for r in range(rows):
        for c in range(cols):
            if (r, c) not in taken:
                cells.append(GridCell(row=r, col=c, rect=(xb[c], yb[r], xb[c + 1], yb[r + 1])))
    return Grid(rect=rect, rows=rows, cols=cols, cells=cells)
