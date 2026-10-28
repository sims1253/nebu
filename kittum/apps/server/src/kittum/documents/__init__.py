"""Documents, parsed layers, and anchors (DESIGN.md, "Provenance that survives re-parsing").

A document is content-addressed. A layer is one parser recipe's immutable reading of it: NFC
text, page geometry in unrotated PDF points, and tables. An anchor points into a layer with a
text quote, a position, boxes, and table cells. Re-parsing never edits a layer; `resolve` maps an
old anchor onto a new layer and says how sure it is, and `remap` records that mapping.

    with ctx.db.transaction() as tx:
        document = documents.ingest(tx, artifacts, pdf)
        layer = documents.derive(tx, document, documents.PyMuPDFParser(), artifacts=artifacts)
        span, = find_quote(quote, layer.text)
        anchor = documents.save_anchor(tx, documents.make_anchor(layer, span))

Parsers: `PyMuPDFParser` is the base layer. `PdfiumParser` reads the same PDF with PDFium for
differential checks (`compare_layers`). `DoclingParser` (the `docling` extra) adds layout and
TableFormer grids over PyMuPDF's characters. `OcrMyPdfParser` OCRs pages triage marks as scans or
broken encodings, when `ocrmypdf` and `tesseract` are installed.

Owner: WP-10.
"""

from kittum.documents.acquisition import ACQUIRED, NOT_ACQUIRED, acquire, acquisitions
from kittum.documents.anchors import (
    RESOLVE_VERSION,
    DifferentDocument,
    compare_layers,
    make_anchor,
    page_words,
    resolve,
)
from kittum.documents.docling_engine import DoclingParser, DoclingUnavailable, docling_available
from kittum.documents.engines import PdfiumParser, PyMuPDFParser
from kittum.documents.model import (
    Acquisition,
    AcquisitionAttempt,
    AcquisitionOutcome,
    Anchor,
    Box,
    CellRef,
    Document,
    FullTextLocation,
    Layer,
    Line,
    Page,
    PageAgreement,
    PageKind,
    PageTriage,
    Parser,
    ParserRecipe,
    RawLayer,
    Resolution,
    Table,
    TableCell,
    TextQuote,
    Word,
)
from kittum.documents.ocr import OcrMyPdfParser, OcrUnavailable, ocr_available
from kittum.documents.render import MAX_SCALE, MIN_SCALE, RENDERER, render_page
from kittum.documents.store import (
    NotAPdf,
    derive,
    get_anchor,
    get_document,
    get_layer,
    ingest,
    list_layers,
    recipe_hash,
    remap,
    save_anchor,
)
from kittum.documents.triage import triage

__all__ = [
    "ACQUIRED",
    "MAX_SCALE",
    "MIN_SCALE",
    "NOT_ACQUIRED",
    "RENDERER",
    "RESOLVE_VERSION",
    "Acquisition",
    "AcquisitionAttempt",
    "AcquisitionOutcome",
    "Anchor",
    "Box",
    "CellRef",
    "DifferentDocument",
    "DoclingParser",
    "DoclingUnavailable",
    "Document",
    "FullTextLocation",
    "Layer",
    "Line",
    "NotAPdf",
    "OcrMyPdfParser",
    "OcrUnavailable",
    "Page",
    "PageAgreement",
    "PageKind",
    "PageTriage",
    "Parser",
    "ParserRecipe",
    "PdfiumParser",
    "PyMuPDFParser",
    "RawLayer",
    "Resolution",
    "Table",
    "TableCell",
    "TextQuote",
    "Word",
    "acquire",
    "acquisitions",
    "compare_layers",
    "derive",
    "docling_available",
    "get_anchor",
    "get_document",
    "get_layer",
    "ingest",
    "list_layers",
    "make_anchor",
    "ocr_available",
    "page_words",
    "recipe_hash",
    "remap",
    "render_page",
    "resolve",
    "save_anchor",
    "triage",
]
