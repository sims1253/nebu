"""The values `documents` hands to other modules.

Offsets are Unicode code points, half-open, over a layer's NFC text. Boxes are in unrotated PDF
points with the origin at the top-left corner of the crop box. Pages are 1-based.
"""

from datetime import datetime
from typing import Literal, Protocol

from kittum.core import Id, Record, Sha256

PageKind = Literal["text", "ocr", "broken_encoding"]
ResolutionStatus = Literal["exact", "candidate", "ambiguous", "unresolved"]


class Document(Record):
    id: Id
    artifact_hash: Sha256
    media_type: str
    page_count: int | None = None


class ParserRecipe(Record):
    engine: str  # "pymupdf", "pypdfium2", "docling", ...
    engine_version: str
    container_digest: str | None = None
    weights: tuple[tuple[str, Sha256], ...] = ()
    settings: tuple[tuple[str, str], ...] = ()


class Box(Record):
    page: int  # 1-based
    x0: float
    y0: float
    x1: float
    y1: float


class Word(Record):
    start: int
    end: int
    box: Box


class Line(Record):
    """One printed line, or one line inside a table cell."""

    start: int
    end: int
    box: Box


class PageTriage(Record):
    """Whether a page's text layer can be trusted (pdf-inspector plus a PyMuPDF check)."""

    page: int
    kind: PageKind
    reasons: tuple[str, ...] = ()


class Page(Record):
    number: int  # 1-based
    width: float  # unrotated
    height: float
    rotation: int = 0  # degrees clockwise the viewer turns the page for display
    frame: int = 0  # degrees clockwise the unrotated page turns so most text reads left to right
    label: str | None = None  # printed page label, when the PDF has one
    start: int  # this page's slice of the layer text
    end: int
    triage: PageTriage | None = None


class Table(Record):
    id: str
    page: int
    box: Box
    rows: int
    cols: int
    start: int
    end: int


class TableCell(Record):
    table_id: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    start: int  # an empty cell has start == end
    end: int
    box: Box
    header: bool = False
    header_path: tuple[str, ...] = ()  # column headers above this cell, outermost first


class RawLayer(Record):
    """What a parser returns. `documents.derive` turns it into a stored `Layer`."""

    text: str
    words: tuple[Word, ...]
    cells: tuple[TableCell, ...] = ()
    lines: tuple[Line, ...] = ()
    pages: tuple[Page, ...] = ()
    tables: tuple[Table, ...] = ()


class Layer(Record):
    id: Id
    document_id: Id
    recipe: ParserRecipe
    recipe_hash: Sha256
    output_hash: Sha256  # SHA-256 of the stored layer JSON, which is also its artifact hash
    text: str
    words: tuple[Word, ...]
    cells: tuple[TableCell, ...] = ()
    lines: tuple[Line, ...] = ()
    pages: tuple[Page, ...] = ()
    tables: tuple[Table, ...] = ()


class TextQuote(Record):
    exact: str
    prefix: str = ""
    suffix: str = ""


class CellRef(Record):
    table_id: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    header_path: tuple[str, ...] = ()


class Anchor(Record):
    id: Id
    layer_id: Id
    document_id: Id
    quote: TextQuote
    text_hash: Sha256  # SHA-256 of the exact quote, UTF-8
    start: int
    end: int
    boxes: tuple[Box, ...]  # one per printed line the quote touches
    cells: tuple[CellRef, ...] = ()


class Resolution(Record):
    status: ResolutionStatus
    anchor: Anchor | None
    method: str  # "same_layer", "quote", "quote_in_region", "fuzzy", "none"
    method_version: str
    alternatives: tuple[Anchor, ...] = ()  # every match, when the status is "ambiguous"


class Parser(Protocol):
    recipe: ParserRecipe

    def parse(self, pdf: bytes) -> RawLayer: ...


class FullTextLocation(Record):
    """One place the full-text waterfall may fetch a PDF from."""

    source: str  # "europepmc", "unpaywall", "openalex", "publisher", ...
    url: str
    licence: str | None = None  # as the source states it, e.g. "cc by"
    terms_url: str | None = None


class ExpectedReport(Record):
    """The record a fetched PDF must be the report of (`kittum.documents.check_report`)."""

    doi: str | None = None
    title: str | None = None
    refused: tuple[str, ...] = ()
    """Artifact hashes of PDFs a reviewer said are not this record's report: refused from any URL."""


class AcquisitionAttempt(Record):
    source: str
    url: str
    status: int | None  # None when the source gave no response
    outcome: Literal[
        "acquired", "http_error", "not_pdf", "unreadable_pdf", "unavailable", "wrong_document"
    ]
    body_hash: Sha256 | None = None
    licence: str | None = None  # the location's, as its source states it
    error: str | None = None  # why an unavailable source gave no response
    check: str | None = None  # what the check against the record found, when it ran
    message: str | None = None  # what the source said about a failure, when it said anything


class Acquisition(Record):
    id: Id
    project_id: Id
    document: Document
    source: str
    url: str | None
    licence: str | None
    terms_url: str | None
    acquired_at: datetime


class AcquisitionOutcome(Record):
    acquisition: Acquisition | None  # None when every location failed
    attempts: tuple[AcquisitionAttempt, ...]


class PageAgreement(Record):
    """How closely two layers of the same PDF agree on one page."""

    page: int
    words_a: int
    words_b: int
    text_similarity: float  # 0..1 over the word sequences
    box_iou: float  # median IoU of the boxes of words both layers read identically
