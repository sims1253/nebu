"""Documents, parsed layers, and anchors (DESIGN.md, "Provenance that survives re-parsing").

A document is content-addressed. A layer is one parser recipe's immutable reading of it: NFC
text, page geometry in unrotated PDF points, and tables. An anchor points into a layer with a
text quote, a position, boxes, and table cells. Re-parsing never edits a layer; `resolve` maps an
old anchor onto a new layer and says how sure it is.

Owner: WP-10. Shipped now: the types and the `Parser` seam. Still to build: page triage with
pdf-inspector, parsers for PyMuPDF, pypdfium2, and Docling, OCRmyPDF for scans, layer storage,
`resolve`, and acquisition through the full-text waterfall.
"""

from typing import Literal, Protocol

from kittum.core import Id, Record, Sha256


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


class TableCell(Record):
    table_id: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    start: int
    end: int
    box: Box


class RawLayer(Record):
    """What a parser returns. `documents.derive` turns it into a stored `Layer`."""

    text: str
    words: tuple[Word, ...]
    cells: tuple[TableCell, ...] = ()


class Layer(Record):
    id: Id
    document_id: Id
    recipe_hash: Sha256
    output_hash: Sha256
    text: str
    words: tuple[Word, ...]
    cells: tuple[TableCell, ...] = ()


class TextQuote(Record):
    exact: str
    prefix: str = ""
    suffix: str = ""


class Anchor(Record):
    id: Id
    layer_id: Id
    quote: TextQuote
    start: int
    end: int
    boxes: tuple[Box, ...]
    cells: tuple[tuple[str, int, int], ...] = ()  # (table_id, row, col)


class Resolution(Record):
    status: Literal["exact", "candidate", "ambiguous", "unresolved"]
    anchor: Anchor | None
    method_version: str


class Parser(Protocol):
    recipe: ParserRecipe

    def parse(self, pdf: bytes) -> RawLayer: ...


def derive(document: Document, parser: Parser) -> Layer:
    raise NotImplementedError("WP-10")


def resolve(anchor: Anchor, layer: Layer) -> Resolution:
    raise NotImplementedError("WP-10")


__all__ = [
    "Anchor",
    "Box",
    "Document",
    "Layer",
    "Parser",
    "ParserRecipe",
    "RawLayer",
    "Resolution",
    "TableCell",
    "TextQuote",
    "Word",
    "derive",
    "resolve",
]
