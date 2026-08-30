"""Models describing a document's pages, tables, and text.

The document reader produces these models; the extractor locates fields
inside them. Neither side depends on how the content was obtained."""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class BoundingBox(BaseModel):
    """Box position and size in PDF points, measured from the page's
    top-left corner."""

    x: float = Field(..., ge=0, description="X coordinate (left edge) in points")
    y: float = Field(..., ge=0, description="Y coordinate (top edge) in points")
    width: float = Field(..., ge=0, description="Width in points")
    height: float = Field(..., ge=0, description="Height in points")
    page: int = Field(..., ge=0, description="0-indexed page number")

    @field_validator("page")
    @classmethod
    def validate_page(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Page number must be non-negative")
        return v


class TextElement(BaseModel):
    """A single text element with optional bounding box and confidence."""

    text: str = Field(..., min_length=1, description="Extracted text content")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score (0.0-1.0)",
    )
    bbox: BoundingBox | None = Field(
        None,
        description="Bounding box coordinates, if available",
    )
    element_type: str = Field(
        "text",
        description="Type of element: text, word, line, cell, etc.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Reader-specific metadata",
    )


class TableCell(BaseModel):
    """A table cell with content and position."""

    text: str = Field(..., description="Cell text content")
    row: int = Field(..., ge=0, description="0-indexed row number")
    column: int = Field(..., ge=0, description="0-indexed column number")
    bbox: BoundingBox | None = Field(
        None,
        description="Bounding box for the cell",
    )
    confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score for cell content",
    )
    section: str | None = Field(
        None,
        description=(
            "Label of the row's group header, when the reader supplies one. "
            "The extractor uses it as row context to tell equally labeled "
            "rows apart. The default text-layer reader leaves it unset."
        ),
    )


class TableStructure(BaseModel):
    """Structured table with cells and boundaries."""

    rows: int = Field(..., ge=0, description="Number of rows")
    columns: int = Field(..., ge=0, description="Number of columns")
    cells: list[TableCell] = Field(
        default_factory=list,
        description="Individual cell contents",
    )
    bbox: BoundingBox | None = Field(
        None,
        description="Bounding box for entire table",
    )
    header_row: bool = Field(
        False,
        description="Whether first row is a header",
    )
    bordered: bool | None = Field(
        None,
        description=(
            "How the table was detected: True when its ruling lines were found "
            "geometrically, False when it was inferred from text alignment "
            "without rules (a borderless rebuild or whitespace guess), None "
            "when the reader did not record it. Prose consumers (text_span "
            "exclusion) trust only geometrically ruled tables — an inferred "
            "'table' laid over running prose must not swallow it."
        ),
    )


class PageContent(BaseModel):
    """Extracted content for a single page."""

    page_number: int = Field(..., ge=0, description="0-indexed page number")
    width: float | None = Field(None, ge=0, description="Page width in points")
    height: float | None = Field(None, ge=0, description="Page height in points")
    text_elements: list[TextElement] = Field(
        default_factory=list,
        description="Extracted text elements",
    )
    full_text: str = Field(
        "",
        description="Concatenated text content for the page",
    )
    tables: list[TableStructure] = Field(
        default_factory=list,
        description="Extracted table structures",
    )
    confidence: float = Field(
        1.0,
        ge=0.0,
        le=1.0,
        description="Overall page confidence",
    )
