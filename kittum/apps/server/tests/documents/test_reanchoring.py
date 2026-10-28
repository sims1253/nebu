"""Anchors made on one layer, re-anchored onto a layer from another engine."""

import pymupdf
import pytest

from kittum.core import Span, find_quote
from kittum.documents import (
    RESOLVE_VERSION,
    DifferentDocument,
    compare_layers,
    derive,
    get_anchor,
    ingest,
    make_anchor,
    remap,
    resolve,
    save_anchor,
)
from kittum.documents.testing import TextParser
from kittum.storage import MemoryStore
from tests.documents.conftest import LESOTHO, VITAMIN_C, squash, text_under
from tests.documents.test_parsing import COLUMN_BREAK_QUOTE

QUOTES = [
    COLUMN_BREAK_QUOTE,  # across a column break and a hyphenated line break
    "a fourfold increase since 1990, with 80% residing in low",  # across a soft hyphen
    "61.4 (11.7)",  # a table cell
    "Primary analysis set",  # a header spanning three columns
]


def _layers(pg, *parsers, pdf=None):
    artifacts = MemoryStore()
    document = ingest(pg, artifacts, pdf or LESOTHO.read_bytes())
    return [derive(pg, document, p, artifacts=artifacts) for p in parsers]


def _anchors(pg, layer, quotes):
    out = []
    for quote in quotes:
        for span in find_quote(quote, layer.text):
            out.append(save_anchor(pg, make_anchor(layer, span)))
    return out


def _sideways_pdf() -> bytes:
    """Page 7 drawn sideways on a landscape page, with no /Rotate."""
    sideways = pymupdf.open()
    source = pymupdf.open(LESOTHO)
    landscape = sideways.new_page(width=source[6].rect.height, height=source[6].rect.width)
    landscape.show_pdf_page(landscape.rect, source, 6, rotate=90)
    return sideways.tobytes()


def test_anchors_survive_a_reparse_with_pdfium(pg, pymupdf_parser, pdfium_parser):
    base, other = _layers(pg, pymupdf_parser, pdfium_parser)
    # "1.0 (1.0, 2.0)" is printed three times in one row; only the place tells them apart.
    anchors = _anchors(pg, base, [*QUOTES, "1.0 (1.0, 2.0)"])
    assert len(anchors) >= len(QUOTES) + 3  # "Primary analysis set" recurs in the text too

    for anchor in anchors:
        resolution = remap(pg, anchor, other)
        assert resolution.status == "exact", anchor.quote.exact
        moved = resolution.anchor
        assert moved is not None and moved.layer_id == other.id
        assert squash(other.text[moved.start : moved.end]) == squash(anchor.quote.exact)
        assert [b.page for b in moved.boxes] == [b.page for b in anchor.boxes]
        for old, new in zip(anchor.boxes, moved.boxes, strict=True):
            assert abs(old.x0 - new.x0) < 2 and abs(old.y0 - new.y0) < 2
    repeated = [a for a in anchors if a.quote.exact == "1.0 (1.0, 2.0)"]
    methods = {remap(pg, a, other).method for a in repeated}
    assert methods == {"quote_in_region"}

    rows = pg.execute(
        "select status, new_anchor_id from anchor_mapping where layer_id = %s", (other.id,)
    ).fetchall()
    assert len(rows) == len(anchors)  # the second remap above reused the recorded mappings
    assert {r["status"] for r in rows} == {"exact"}
    assert get_anchor(pg, rows[0]["new_anchor_id"]).layer_id == other.id


def test_moved_repeated_changed_and_respelled_quotes(pg, pymupdf_parser, pdfium_parser):
    base, other = _layers(pg, pymupdf_parser, pdfium_parser)
    (age,) = _anchors(pg, base, ["61.4 (11.7)"])
    # As if the old layer had placed the value on page 3: the only match is elsewhere.
    elsewhere = tuple(b.model_copy(update={"page": 3}) for b in age.boxes)
    moved = resolve(age.model_copy(update={"boxes": elsewhere}), other)
    assert (moved.status, moved.method) == ("candidate", "quote")

    first, *_ = _anchors(pg, base, ["1.0 (1.0, 2.0)"])
    lost = resolve(first.model_copy(update={"boxes": elsewhere}), other)
    assert lost.status == "ambiguous" and lost.anchor is None
    assert len(lost.alternatives) == 3

    changed = age.model_copy(
        update={"quote": age.quote.model_copy(update={"exact": "62.4 (11.7)"})}
    )
    assert resolve(changed, other).status == "unresolved"  # a changed number never matches

    (fourfold,) = _anchors(pg, base, [QUOTES[1]])
    respelled = fourfold.model_copy(
        update={
            "quote": fourfold.quote.model_copy(
                update={"exact": "fourfold increase since 1990 with 80 % residing in low"}
            )
        }
    )
    fuzzy = resolve(respelled, other)
    assert (fuzzy.status, fuzzy.method) == ("candidate", "fuzzy")
    assert (
        fuzzy.anchor is not None and "fourfold" in other.text[fuzzy.anchor.start : fuzzy.anchor.end]
    )


def test_pymupdf_and_pdfium_agree_on_every_page(pg, pymupdf_parser, pdfium_parser):
    base, other = _layers(pg, pymupdf_parser, pdfium_parser)
    agreement = compare_layers(base, other)
    assert len(agreement) == 13
    for page in agreement:
        assert page.text_similarity > 0.9, page
        assert page.box_iou > 0.9, page


def test_turned_pages_read_in_order_with_boxes_in_the_unrotated_frame(
    pg, pymupdf_parser, pdfium_parser
):
    plain = _layers(pg, pymupdf_parser)[0]
    (span,) = find_quote("61.4 (11.7)", plain.text)
    (expected,) = make_anchor(plain, span).boxes

    # An upright page the viewer turns with /Rotate 90: boxes stay where they were drawn.
    doc = pymupdf.open(LESOTHO)
    doc[6].set_rotation(90)
    flagged = doc.tobytes()
    for layer in _layers(pg, pymupdf_parser, pdfium_parser, pdf=flagged):
        page = layer.pages[6]
        assert (page.rotation, page.width, page.height) == (
            90,
            plain.pages[6].width,
            plain.pages[6].height,
        )
        (span,) = find_quote("61.4 (11.7)", layer.text)
        (box,) = make_anchor(layer, span).boxes
        for got, want in zip(
            box.model_dump().values(), expected.model_dump().values(), strict=True
        ):
            assert abs(got - want) < 1.5, layer.recipe.engine

    # A landscape page with the article drawn sideways and no /Rotate: the reader turns their
    # head, and so does the reading order. Columns and table rows still read in order.
    turned = _sideways_pdf()
    for layer in _layers(pg, pymupdf_parser, pdfium_parser, pdf=turned):
        assert layer.pages[0].rotation == 0
        (span,) = find_quote(COLUMN_BREAK_QUOTE, layer.text)
        assert len(make_anchor(layer, span).boxes) == 3, layer.recipe.engine
        assert find_quote("Age, years, mean (SD) 61.4 (11.7) 63.2 (14.0)", layer.text)
        (span,) = find_quote("61.4 (11.7)", layer.text)
        (box,) = make_anchor(layer, span).boxes
        assert box.x1 - box.x0 < box.y1 - box.y0  # a sideways word is a tall box
        assert text_under(turned, box) == "61.4 (11.7)", layer.recipe.engine


def test_docling_reparse_keeps_table_and_text_anchors(pg, pymupdf_parser, docling_page7):
    base, docling = _layers(pg, pymupdf_parser, docling_page7)
    assert docling.recipe.engine == "docling" and len(docling.recipe.weights) == 2

    table = next(t for t in docling.tables if t.page == 7)
    header = next(c for c in docling.cells if c.table_id == table.id and c.row == 0 and c.col == 1)
    assert squash(docling.text[header.start : header.end]) == "Primary analysis set"
    assert header.col_span == 3  # TableFormer's span, text from PyMuPDF

    for anchor in _anchors(pg, base, [COLUMN_BREAK_QUOTE, "61.4 (11.7)", "1.0 (1.0, 2.0)"]):
        resolution = remap(pg, anchor, docling)
        assert resolution.status == "exact", anchor.quote.exact
        assert resolution.method_version == RESOLVE_VERSION
        assert resolution.anchor is not None
        old = [(c.row, c.col, c.header_path) for c in anchor.cells]
        new = [(c.row, c.col, c.header_path) for c in resolution.anchor.cells]
        assert new == old


@pytest.mark.parametrize("quote", ["61.4 (11.7)", COLUMN_BREAK_QUOTE])
def test_an_anchor_resolves_onto_its_own_layer(pg, pymupdf_parser, quote):
    (layer,) = _layers(pg, pymupdf_parser)
    (anchor,) = _anchors(pg, layer, [quote])
    resolution = resolve(anchor, layer)
    assert (resolution.status, resolution.method, resolution.anchor) == (
        "exact",
        "same_layer",
        anchor,
    )


def _text_layers(pg, old: str, new: str):
    """Two layers of one document whose text differs, laid out identically."""
    artifacts = MemoryStore()
    document = ingest(pg, artifacts, LESOTHO.read_bytes())
    return [
        derive(pg, document, TextParser([text], name=name), artifacts=artifacts)
        for name, text in (("old", old), ("new", new))
    ]


@pytest.mark.parametrize(
    ("new_text", "status"),
    [
        ("Baseline: N = 123 patients were randomized.", "unresolved"),  # 12 became 123
        ("Baseline: N = 12.5 patients were randomized.", "unresolved"),  # 12 became 12.5
        ("Baseline: N = 412 patients were randomized.", "unresolved"),  # 12 became 412
        ("Baseline: N = 12 vials were randomized.", "candidate"),  # same number, new context
        ("Baseline: N = -12 patients were randomized.", "candidate"),  # a sign before it
        ("Baseline: N = 12 patients were randomized.", "exact"),
        ("Baseline: N =  12   patients were\nrandomized.", "exact"),  # spacing only
    ],
)
def test_a_changed_number_or_context_is_never_exact(pg, new_text, status):
    old, new = _text_layers(pg, "Baseline: N = 12 patients were randomized.", new_text)
    (span,) = find_quote("12", old.text)
    resolution = resolve(save_anchor(pg, make_anchor(old, span)), new)
    assert resolution.status == status
    if resolution.anchor is not None:
        assert new.text[resolution.anchor.start : resolution.anchor.end] == "12"


def test_a_changed_word_beside_the_quote_is_never_exact(pg):
    old, new = _text_layers(pg, "There were 12 nonfatal events.", "There were 12 fatal events.")
    (span,) = find_quote("12", old.text)
    assert resolve(save_anchor(pg, make_anchor(old, span)), new).status == "candidate"


def test_resolve_refuses_a_layer_of_another_document(pg):
    artifacts = MemoryStore()
    text = ["Baseline: N = 12 patients were randomized."]
    # Same quote, same geometry, different PDF bytes: a revision is a different document.
    first, revised = (
        derive(pg, ingest(pg, artifacts, path.read_bytes()), TextParser(text), artifacts=artifacts)
        for path in (LESOTHO, VITAMIN_C)
    )
    (span,) = find_quote("12 patients", first.text)
    anchor = save_anchor(pg, make_anchor(first, span))
    with pytest.raises(DifferentDocument):
        resolve(anchor, revised)
    with pytest.raises(DifferentDocument):
        remap(pg, anchor, revised)
    assert pg.execute("select count(*) as n from anchor_mapping").fetchone() == {"n": 0}


def test_part_of_a_sideways_word_is_cut_along_its_reading_direction(
    pg, pymupdf_parser, pdfium_parser
):
    turned = _sideways_pdf()
    for layer in _layers(pg, pymupdf_parser, pdfium_parser, pdf=turned):
        assert layer.pages[0].frame == 90
        (span,) = find_quote("61.4 (11.7)", layer.text)
        word = next(w for w in layer.words if w.start == span.start)
        assert layer.text[word.start : word.end] == "61.4"
        (box,) = make_anchor(layer, Span(span.start, span.start + 2)).boxes  # "61"
        assert (box.x0, box.x1) == (word.box.x0, word.box.x1)  # the full line height
        assert abs((box.y1 - box.y0) - (word.box.y1 - word.box.y0) / 2) < 0.1  # half the length
        assert text_under(turned, box) == "61", layer.recipe.engine
