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

from kittum.documents.acquisition import (
    ACQUIRED,
    ATTEMPTED,
    NOT_ACQUIRED,
    SOURCE_NAMES,
    FetchedLocation,
    acquire,
    acquisitions,
    fetch_location,
    get_acquisition,
    record_location,
    source_name,
)
from kittum.documents.anchors import (
    RESOLVE_VERSION,
    DifferentDocument,
    compare_layers,
    make_anchor,
    page_words,
    quote_spans,
    resolve,
)
from kittum.documents.docling_engine import DoclingParser, DoclingUnavailable, docling_available
from kittum.documents.engines import PdfiumParser, PyMuPDFParser
from kittum.documents.matching import (
    TITLE_THRESHOLD,
    ReportCheck,
    check_pages,
    check_report,
    first_pages,
)
from kittum.documents.model import (
    Acquisition,
    AcquisitionAttempt,
    AcquisitionOutcome,
    Anchor,
    Box,
    CellRef,
    Document,
    ExpectedReport,
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
    "ATTEMPTED",
    "MAX_SCALE",
    "MIN_SCALE",
    "NOT_ACQUIRED",
    "RENDERER",
    "RESOLVE_VERSION",
    "SOURCE_NAMES",
    "TITLE_THRESHOLD",
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
    "ExpectedReport",
    "FetchedLocation",
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
    "ReportCheck",
    "Resolution",
    "Table",
    "TableCell",
    "TextQuote",
    "Word",
    "acquire",
    "acquisitions",
    "check_pages",
    "check_report",
    "compare_layers",
    "derive",
    "docling_available",
    "fetch_location",
    "first_pages",
    "get_acquisition",
    "get_anchor",
    "get_document",
    "get_layer",
    "ingest",
    "list_layers",
    "make_anchor",
    "ocr_available",
    "page_words",
    "quote_spans",
    "recipe_hash",
    "record_location",
    "remap",
    "render_page",
    "resolve",
    "save_anchor",
    "source_name",
    "triage",
]
