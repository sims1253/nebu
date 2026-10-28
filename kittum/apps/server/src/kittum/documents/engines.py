"""Parsers: PyMuPDF (the base layer) and pypdfium2 (the second engine for differential checks).

Both read words with boxes in the page's reading frame and hand them to `layout.assemble`, which
writes the canonical text and maps boxes back to the unrotated frame.
"""

import math
from functools import cache

import pymupdf
import pypdfium2
import pypdfium2.raw as pdfium_c
from pypdfium2.version import PDFIUM_INFO, PYPDFIUM_INFO

from kittum.documents.layout import (
    ASSEMBLY_VERSION,
    Grid,
    PageInput,
    PBlock,
    PWord,
    Rect,
    assemble,
    contains,
    reading_frame,
    rotate,
)
from kittum.documents.model import ParserRecipe, RawLayer
from kittum.documents.tables import (
    TABLES_VERSION,
    caption_regions,
    grid_from_words,
    horizontal_rules,
)
from kittum.documents.triage import triage

# Word extraction without TEXT_PRESERVE_LIGATURES, so "ﬁ" arrives as "fi".
_WORD_FLAGS = pymupdf.TEXTFLAGS_WORDS & ~pymupdf.TEXT_PRESERVE_LIGATURES


@cache
def _quiet() -> None:
    # PyMuPDF prints a recommendation for its Layout package on first table detection.
    pymupdf.no_recommend_layout()


def _unrotated_size(page: pymupdf.Page) -> tuple[float, float]:
    w, h = page.rect.width, page.rect.height
    return (h, w) if page.rotation % 180 else (w, h)


def pymupdf_frame(page: pymupdf.Page) -> int:
    """The page's reading frame from PyMuPDF's line directions (reported unrotated)."""
    directions = [
        (line["dir"][0], line["dir"][1], sum(len(s["text"]) for s in line["spans"]))
        for block in page.get_text("dict", flags=_WORD_FLAGS)["blocks"]
        for line in block.get("lines", ())
    ]
    return reading_frame(directions)


def pymupdf_blocks(page: pymupdf.Page, frame: int) -> list[PBlock]:
    """Words grouped by PyMuPDF's own blocks and lines, turned into the reading frame."""
    width, height = _unrotated_size(page)
    blocks: dict[int, dict[int, list[PWord]]] = {}
    for x0, y0, x1, y1, text, block_no, line_no, _ in page.get_text("words", flags=_WORD_FLAGS):
        rect = rotate((x0, y0, x1, y1), frame, width, height)
        blocks.setdefault(block_no, {}).setdefault(line_no, []).append(PWord(text=text, rect=rect))
    return [PBlock(lines=[lines[k] for k in sorted(lines)]) for _, lines in sorted(blocks.items())]


def _segments(page: pymupdf.Page, frame: int) -> list[Rect]:
    """Stroked lines and filled rectangles, turned into the reading frame."""
    width, height = _unrotated_size(page)
    out: list[Rect] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", ()):
            if item[0] == "l":
                p, q = item[1], item[2]
                r = (min(p.x, q.x), min(p.y, q.y), max(p.x, q.x), max(p.y, q.y))
            elif item[0] == "re":
                r = tuple(item[1])
            else:
                continue
            out.append(rotate((r[0], r[1], r[2], r[3]), frame, width, height))
    return out


def pymupdf_grids(page: pymupdf.Page, blocks: list[PBlock], frame: int) -> list[Grid]:
    """Ruled tables from `find_tables` without the Layout model, and three-rule tables found from
    their captions. Both get their grid from the words."""
    _quiet()
    width, height = _unrotated_size(page)
    rules = horizontal_rules(_segments(page, frame))
    regions = caption_regions(blocks, rules)
    try:
        # find_tables reads the page as drawn, so it only helps when no turn is needed.
        found = page.find_tables(use_layout=False).tables if frame == 0 else []
    except AttributeError, ValueError, RuntimeError:
        found = []
    for table in found:
        b = tuple(table.bbox)
        r = rotate((b[0], b[1], b[2], b[3]), frame, width, height)
        if (
            table.row_count >= 2
            and table.col_count >= 2
            and not any(
                min(r[2], q[2]) > max(r[0], q[0]) and min(r[3], q[3]) > max(r[1], q[1])
                for q in regions
            )
        ):
            regions.append(r)
    words = [w for b in blocks for line in b.lines for w in line]
    grids: list[Grid] = []
    for region in regions:
        inside = [w for w in words if contains(region, w.cx, w.cy)]
        grid = grid_from_words(region, inside, rules) if inside else None
        if grid is not None:
            grids.append(grid)
    return grids


def pymupdf_page_input(page: pymupdf.Page, *, with_tables: bool = True) -> PageInput:
    frame = pymupdf_frame(page)
    width, height = _unrotated_size(page)
    if frame % 180:
        width, height = height, width
    blocks = pymupdf_blocks(page, frame)
    return PageInput(
        number=page.number + 1,  # ty: ignore[unsupported-operator]
        width=width,
        height=height,
        rotation=page.rotation,
        frame=frame,
        label=page.get_label() or None,
        blocks=blocks,
        grids=pymupdf_grids(page, blocks, frame) if with_tables else [],
    )


class PyMuPDFParser:
    """The base layer: PyMuPDF characters, Kittum's reading order and table grids."""

    def __init__(self) -> None:
        self.recipe = ParserRecipe(
            engine="pymupdf",
            engine_version=f"{pymupdf.VersionBind} (MuPDF {pymupdf.VersionFitz})",
            settings=(
                ("assembly", ASSEMBLY_VERSION),
                ("tables", TABLES_VERSION),
                ("find_tables.use_layout", "false"),
                ("triage", "pdf-inspector+pymupdf.v1"),
            ),
        )

    def parse(self, pdf: bytes) -> RawLayer:
        pages = triage(pdf)
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            inputs = []
            for page in doc:
                page_input = pymupdf_page_input(page)
                page_input.triage = pages[page_input.number - 1]
                inputs.append(page_input)
        return assemble(inputs)


def _pdfium_char(textpage: pypdfium2.PdfTextPage, i: int) -> str:
    code = pdfium_c.FPDFText_GetUnicode(textpage, i)
    if code == 2:  # PDFium marks a hyphen it found at a line end as U+0002
        return "-"
    if code in (0xFFFE, 0xFFFF) or (code < 32 and code not in (9, 10, 13)):
        return ""
    return chr(code)


def _pdfium_chars(page: pypdfium2.PdfPage) -> list[tuple[str, Rect, float]]:
    """Each character with its loose box in the unrotated top-left frame and its angle."""
    left, _, _, top = page.get_cropbox()
    textpage = page.get_textpage()
    chars: list[tuple[str, Rect, float]] = []
    for i in range(textpage.count_chars()):
        ch = _pdfium_char(textpage, i)
        if ch and not ch.isspace():
            cx0, cy0, cx1, cy1 = textpage.get_charbox(i, loose=True)
            angle = pdfium_c.FPDFText_GetCharAngle(textpage, i)
            chars.append((ch, (cx0 - left, top - cy1, cx1 - left, top - cy0), angle))
        else:
            chars.append((ch, (0.0, 0.0, 0.0, 0.0), 0.0))
    textpage.close()
    return chars


def pdfium_page_input(page: pypdfium2.PdfPage, number: int) -> PageInput:
    """Characters joined into words, lines, and blocks in content order, in the reading frame."""
    left, bottom, right, top = page.get_cropbox()
    width, height = right - left, top - bottom
    chars = _pdfium_chars(page)
    # PDFium's angle turns clockwise in PDF space (y up), which is counter-clockwise once y
    # points down, so the baseline direction in the frame is (cos, sin).
    frame = reading_frame(
        [(math.cos(a), math.sin(a), 1) for ch, _, a in chars if ch and not ch.isspace()]
    )
    lines: list[list[PWord]] = []
    line: list[PWord] = []
    line_rect: list[float] | None = None
    word_chars: list[str] = []
    word_rect: list[float] | None = None
    last_x0 = 0.0

    def grow(acc: list[float] | None, r: Rect) -> list[float]:
        if acc is None:
            return list(r)
        return [min(acc[0], r[0]), min(acc[1], r[1]), max(acc[2], r[2]), max(acc[3], r[3])]

    def end_word() -> None:
        nonlocal word_chars, word_rect
        if word_chars and word_rect is not None:
            r = word_rect
            line.append(PWord(text="".join(word_chars), rect=(r[0], r[1], r[2], r[3])))
        word_chars, word_rect = [], None

    def end_line() -> None:
        nonlocal line, line_rect
        end_word()
        if line:
            lines.append(line)
        line, line_rect = [], None

    for ch, unrotated, _ in chars:
        if ch in ("\r", "\n"):
            end_line()
            continue
        if not ch or ch.isspace():
            end_word()
            continue
        rect: Rect = rotate(unrotated, frame, width, height)
        h = max(rect[3] - rect[1], 0.1)
        if line_rect is not None:
            overlap = min(line_rect[3], rect[3]) - max(line_rect[1], rect[1])
            if overlap < 0.5 * min(h, line_rect[3] - line_rect[1]) or rect[0] < last_x0 - h:
                end_line()
            elif word_rect is not None and rect[0] - word_rect[2] > 0.1 * h:
                end_word()
        word_chars.append(ch)
        word_rect = grow(word_rect, rect)
        line_rect = grow(line_rect, rect)
        last_x0 = rect[0]
    end_line()

    blocks: list[PBlock] = []
    for ln in lines:
        x0 = min(w.rect[0] for w in ln)
        x1 = max(w.rect[2] for w in ln)
        y0 = min(w.rect[1] for w in ln)
        h = max(w.rect[3] for w in ln) - y0
        if blocks:
            prev = blocks[-1].lines[-1]
            px0 = min(w.rect[0] for w in prev)
            px1 = max(w.rect[2] for w in prev)
            gap = y0 - max(w.rect[3] for w in prev)
            if -0.5 * h <= gap < 0.7 * h and min(x1, px1) > max(x0, px0):
                blocks[-1].lines.append(ln)
                continue
        blocks.append(PBlock(lines=[ln]))
    if frame % 180:
        width, height = height, width
    return PageInput(
        number=number,
        width=width,
        height=height,
        rotation=page.get_rotation(),
        frame=frame,
        blocks=blocks,
    )


class PdfiumParser:
    """PDFium characters through the same assembly; no table detection."""

    def __init__(self) -> None:
        self.recipe = ParserRecipe(
            engine="pypdfium2",
            engine_version=f"pypdfium2 {PYPDFIUM_INFO} (PDFium {PDFIUM_INFO})",
            settings=(("assembly", ASSEMBLY_VERSION), ("charbox", "loose")),
        )

    def parse(self, pdf: bytes) -> RawLayer:
        doc = pypdfium2.PdfDocument(pdf)
        try:
            inputs = []
            for index in range(len(doc)):
                page = doc[index]
                inputs.append(pdfium_page_input(page, index + 1))
                page.close()
        finally:
            doc.close()
        return assemble(inputs)
