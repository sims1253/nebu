"""Parsing real trial PDFs into layers, and anchoring quotes and table cells in them."""

import re

import psycopg
import pymupdf
import pytest

from kittum.core import find_quote
from kittum.documents import (
    OcrMyPdfParser,
    OcrUnavailable,
    PyMuPDFParser,
    derive,
    ingest,
    make_anchor,
    ocr_available,
    triage,
)
from kittum.storage import MemoryStore
from tests.documents.conftest import LESOTHO, VITAMIN_C, CachedParser, squash, text_under

GUTTER = 297.6  # the middle of the BMC two-column page, in points

COLUMN_BREAK_QUOTE = (
    "Effect estimates were directionally consistent across sensitivity analyses and aligned with "
    "findings from a sister trial in the same setting"
)


def _layer(pg, parser, path):
    artifacts = MemoryStore()
    document = ingest(pg, artifacts, path.read_bytes())
    return derive(pg, document, parser, artifacts=artifacts)


def test_quote_across_a_column_break_gets_one_box_per_line_in_both_columns(pg, pymupdf_parser):
    layer = _layer(pg, pymupdf_parser, LESOTHO)
    (span,) = find_quote(COLUMN_BREAK_QUOTE, layer.text)
    anchor = make_anchor(layer, span)

    # Two lines at the foot of the left column ("consis-" is hyphenated), one at the top of the right.
    assert [b.page for b in anchor.boxes] == [7, 7, 7]
    first, second, third = anchor.boxes
    assert first.x1 < GUTTER and second.x1 < GUTTER and third.x0 > GUTTER
    assert first.y0 < second.y0 and third.y0 < first.y0
    # A different engine reads exactly the quoted words inside the boxes, line by line.
    pdf = LESOTHO.read_bytes()
    assert text_under(pdf, first) == "Effect estimates were directionally consis-"
    assert text_under(pdf, second) == "tent across sensitivity analyses and aligned with findings"
    assert text_under(pdf, third) == "from a sister trial in the same setting"
    assert anchor.quote.prefix.endswith("lower glycae-\nmic levels. ")
    assert anchor.quote.suffix.startswith(" focused on hyper-")
    assert anchor.cells == ()


def test_quote_across_a_soft_hyphenated_line_break(pg, pymupdf_parser):
    layer = _layer(pg, pymupdf_parser, LESOTHO)
    # The PDF breaks "resid-ing" with a soft hyphen; the quote has the whole word.
    (span,) = find_quote("a fourfold increase since 1990, with 80% residing in low", layer.text)
    anchor = make_anchor(layer, span)
    first, second = anchor.boxes
    assert first.page == second.page == 2
    assert first.y0 + 8 < second.y0 < first.y1  # the next line down (line boxes overlap a little)
    assert second.x0 < first.x0  # which starts at the column margin
    pdf = LESOTHO.read_bytes()
    assert text_under(pdf, first).endswith("since 1990, with 80% resid-")
    assert text_under(pdf, second) == "ing in low"


def test_table_cell_value_anchors_to_its_cell_under_merged_headers(pg, pymupdf_parser):
    layer = _layer(pg, pymupdf_parser, LESOTHO)
    table = next(t for t in layer.tables if t.page == 7)
    cells = {(c.row, c.col): c for c in layer.cells if c.table_id == table.id}

    def text(cell):
        return squash(layer.text[cell.start : cell.end])

    # "Primary analysis set" and "All participants set" each span three arms.
    assert (text(cells[0, 1]), cells[0, 1].col_span) == ("Primary analysis set", 3)
    assert (text(cells[0, 4]), cells[0, 4].col_span) == ("All participants set", 3)
    assert [text(cells[1, c]) for c in range(1, 7)] == ["Control", "Intervention", "Total"] * 2

    (span,) = find_quote("61.4 (11.7)", layer.text)  # age, control arm, primary analysis set
    anchor = make_anchor(layer, span)
    (ref,) = anchor.cells
    assert (ref.table_id, ref.col) == (table.id, 1)
    assert ref.header_path == ("Primary analysis set", "Control")
    assert text(cells[ref.row, 0]) == "Age, years, mean (SD)"
    assert text(cells[ref.row, 4]) == "61.2 (13.9)"  # the same row, all participants
    (box,) = anchor.boxes
    cell_box = cells[ref.row, ref.col].box
    assert cell_box.x0 <= box.x0 and box.x1 <= cell_box.x1
    assert cell_box.y0 <= box.y0 and box.y1 <= cell_box.y1
    assert text_under(LESOTHO.read_bytes(), box) == "61.4 (11.7)"


def test_row_spanning_header_cells_and_two_tables_on_a_page(pg, pymupdf_parser):
    layer = _layer(pg, pymupdf_parser, VITAMIN_C)
    on_page_7 = [t for t in layer.tables if t.page == 7]
    assert len(on_page_7) == 2  # Table 4 and Table 5
    table5 = on_page_7[1]
    cells = {(c.row, c.col): c for c in layer.cells if c.table_id == table5.id}
    parameter = cells[0, 0]
    assert squash(layer.text[parameter.start : parameter.end]) == "Parameter"
    assert parameter.row_span == 2 and parameter.header

    (span,) = find_quote("1.228 (1.067, 1.388)", layer.text)  # SMD, urinary vitamin C, postbiopsy
    (ref,) = make_anchor(layer, span).cells
    assert ref.table_id == table5.id
    assert ref.header_path == ("Between-Group Difference", "SMD (95% CI)")
    assert squash(layer.text[cells[ref.row, 1].start : cells[ref.row, 1].end]) == "Postbiopsy"


def _mixed_pdf() -> bytes:
    """Page 1 a born-digital page, page 2 with a ToUnicode map that maps every glyph to a
    private-use character, page 3 an image of a page with no text layer."""
    good = pymupdf.open(LESOTHO)
    broken = pymupdf.open()
    broken.insert_pdf(pymupdf.open(VITAMIN_C), from_page=1, to_page=1)
    for xref in range(1, broken.xref_length()):
        kind, value = broken.xref_get_key(xref, "ToUnicode")
        if kind == "xref":
            stream = int(value.split()[0])
            cmap = broken.xref_stream(stream).decode("latin-1")
            cmap = re.sub(
                r"(beginbf(?:char|range).*?endbf(?:char|range))",
                lambda m: re.sub(r"<[0-9A-Fa-f]+>(\s*)$", r"<E000>\1", m.group(1), flags=re.M),
                cmap,
                flags=re.S,
            )
            broken.update_stream(stream, cmap.encode("latin-1"))
    out = pymupdf.open()
    out.insert_pdf(good, from_page=1, to_page=1)
    out.insert_pdf(pymupdf.open("pdf", broken.tobytes()))
    scan = out.new_page(width=good[2].rect.width, height=good[2].rect.height)
    scan.insert_image(scan.rect, pixmap=good[2].get_pixmap(dpi=100))
    return out.tobytes()


def test_triage_marks_scanned_and_broken_pages_and_the_layer_keeps_it(pg):
    pdf = _mixed_pdf()
    pages = triage(pdf)
    assert [p.kind for p in pages] == ["text", "broken_encoding", "ocr"]
    assert "suspected_garbled_text" in pages[1].reasons  # pdf-inspector's verdict
    assert "scanned" in pages[2].reasons

    artifacts = MemoryStore()
    layer = derive(pg, ingest(pg, artifacts, pdf), PyMuPDFParser(), artifacts=artifacts)
    assert [p.triage.kind if p.triage else None for p in layer.pages] == [
        "text",
        "broken_encoding",
        "ocr",
    ]
    # The scan has no words, and the broken page's text cannot ground a quote.
    assert layer.pages[2].start == layer.pages[2].end
    assert find_quote("randomized", layer.text[layer.pages[1].start : layer.pages[1].end]) == ()


def test_ocr_gives_the_scanned_page_a_text_layer(pg):
    if not ocr_available():
        with pytest.raises(OcrUnavailable):
            OcrMyPdfParser()
        pytest.skip("ocrmypdf and tesseract are not installed; the OCR path refuses to start")
    pdf = _mixed_pdf()
    artifacts = MemoryStore()
    layer = derive(pg, ingest(pg, artifacts, pdf), OcrMyPdfParser(), artifacts=artifacts)
    scanned = layer.text[layer.pages[2].start : layer.pages[2].end]
    assert find_quote("Study design", scanned) or len(scanned.split()) > 200


def test_derive_is_idempotent_per_recipe_and_layers_are_immutable(
    pg, pymupdf_parser, pdfium_parser
):
    artifacts = MemoryStore()
    pdf = LESOTHO.read_bytes()
    document = ingest(pg, artifacts, pdf)
    assert ingest(pg, artifacts, pdf).id == document.id
    assert document.page_count == 13

    counting = CachedParser(pymupdf_parser.inner)
    first = derive(pg, document, counting, artifacts=artifacts)
    again = derive(pg, document, counting, artifacts=artifacts)
    assert counting.calls == 1
    assert again.id == first.id and again.output_hash == first.output_hash
    assert artifacts.exists(first.output_hash)

    other = derive(pg, document, pdfium_parser, artifacts=artifacts)
    assert other.id != first.id and other.recipe.engine == "pypdfium2"

    with pytest.raises(psycopg.errors.RaiseException, match="immutable"), pg.transaction():
        pg.execute("update layer set text_hash = %s where id = %s", ("0" * 64, first.id))
