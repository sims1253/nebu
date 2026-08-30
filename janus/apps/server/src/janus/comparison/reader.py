"""Reads a source document into DocumentEvidence.

`DocumentReader` is the interface. `TextLayerDocumentReader` is the default
implementation and works from the PDF text layer alone.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Protocol

import pymupdf

from janus.comparison.content import (
    BoundingBox,
    PageContent,
    TableCell,
    TableStructure,
    TextElement,
)
from janus.comparison.form_lines import build_form_table, usable_label_pairs
from janus.comparison.models import DocumentEvidence
from janus.comparison.word_tables import rebuild_fragmented_tables

if TYPE_CHECKING:
    from pathlib import Path


class DocumentReader(Protocol):
    def read(self, pdf: Path, document_id: str) -> DocumentEvidence: ...


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.split("\n") if line.strip()]


# A value line that opens with a bracket, an operator, or a compared
# statistic token ("[0.22, 0.31];", "P<0.001", "(0.024)", "BF>100") continues
# the previous line instead of starting a new value.
_WRAPPED_VALUE_LINE = re.compile(r"^(?:[\[(;=<>~\u00b1]|[A-Za-z]{1,4}\s*[<>=\u2264\u2265])")


def _wraps_single_value(text: str) -> bool:
    """Report whether a multi-line cell wraps one value across lines rather
    than stacking one value per line."""
    lines = _split_lines(text)
    return len(lines) >= 2 and any(_WRAPPED_VALUE_LINE.match(line) for line in lines[1:])


def _expand_grouped_rows(table: TableStructure) -> TableStructure:
    """Split merged grouped rows into one row per sub-entry.

    Summary tables often merge a group and its sub-rows into one physical
    row: the label cell reads "Group\nSub A\nSub B" and the value cell
    "1 (2%)\n3 (4%)". When every non-empty value cell in a row is multi-line
    and the line counts align with the label cell (equal, or one fewer, so
    the first label line is the group name), split the row into sub-rows
    and stamp each cell with the group name in `section`. A merely wrapped
    label stays untouched: wrapping pairs a multi-line label with
    single-line values.
    """
    cells = {(cell.row, cell.column): cell for cell in table.cells}
    columns = sorted({cell.column for cell in table.cells})
    out_cells: list[TableCell] = []
    next_row = 0
    for row in sorted({cell.row for cell in table.cells}):
        row_cells = [cells.get((row, column)) for column in columns]
        label_cell = cells.get((row, 0))
        if table.header_row and row == 0:
            splittable = False
        else:
            value_cells = [
                cell
                for cell in row_cells
                if cell is not None and cell.column != 0 and cell.text.strip()
            ]
            label_lines = _split_lines(label_cell.text) if label_cell else []
            line_counts = {len(_split_lines(cell.text)) for cell in value_cells}
            splittable = (
                len(label_lines) >= 2
                and bool(value_cells)
                and len(line_counts) == 1
                and (line_count := next(iter(line_counts))) >= 2
                and line_count in (len(label_lines), len(label_lines) - 1)
                # A cell that wraps one value ("0.26\\n[0.22, 0.31];\\nP<0.001")
                # is not a stack of sub-values; its row is a wrapped label
                # row, not a merged group.
                and not any(_wraps_single_value(cell.text) for cell in value_cells)
            )
        if not splittable:
            for cell in row_cells:
                if cell is not None:
                    out_cells.append(cell.model_copy(update={"row": next_row}))
            next_row += 1
            continue
        if next(iter(line_counts)) == len(label_lines) - 1:
            section: str | None = label_lines[0]
            sub_labels = label_lines[1:]
        else:
            section = None
            sub_labels = label_lines
        for offset, label in enumerate(sub_labels):
            for cell in row_cells:
                if cell is None:
                    continue
                if cell.column == 0:
                    out_cells.append(
                        TableCell(
                            text=label,
                            row=next_row,
                            column=0,
                            confidence=cell.confidence,
                            bbox=cell.bbox,
                            section=section,
                        )
                    )
                else:
                    lines = _split_lines(cell.text)
                    out_cells.append(
                        TableCell(
                            text=lines[offset] if offset < len(lines) else "",
                            row=next_row,
                            column=cell.column,
                            confidence=cell.confidence,
                            bbox=cell.bbox,
                            section=section,
                        )
                    )
            next_row += 1
    return table.model_copy(update={"cells": out_cells, "rows": next_row, "columns": table.columns})


def _bbox_from_rect(
    rect: tuple[float, float, float, float] | None, page_number: int
) -> BoundingBox | None:
    """A detector rectangle (x0, y0, x1, y1 from the page's top-left) as a
    BoundingBox, clamped to the page the way text-line boxes are. A spanned
    grid position has no rectangle of its own and stays boxless."""
    if rect is None:
        return None
    x0, y0, x1, y1 = rect
    return BoundingBox(
        x=max(0.0, x0),
        y=max(0.0, y0),
        width=max(0.0, x1 - x0),
        height=max(0.0, y1 - y0),
        page=page_number,
    )


def _row_cells(table: TableStructure, row: int) -> dict[int, TableCell]:
    return {cell.column: cell for cell in table.cells if cell.row == row}


def _carry_split_header(previous: TableStructure, current: TableStructure) -> TableStructure:
    """Restore the column headers of a table that continues on this page.

    A table that breaks across a page boundary re-opens with the spilled
    remainder of the previous row's label as its first row and no header of
    its own, so every cell on the continuation page carries an empty column
    context. When the first row holds a label and nothing else, the grids
    match, and the previous page's table ends on a wrapped label that could
    have spilled, replace the spill row with the previous table's header row.
    The spilled text stays available in the page's text evidence.
    """
    first = _row_cells(current, 0)
    columns = sorted({cell.column for cell in current.cells})
    label = first.get(0)
    previous_last_label = _row_cells(
        previous, max((cell.row for cell in previous.cells), default=0)
    ).get(0)
    if (
        previous.columns != current.columns
        or current.columns < 2
        or current.rows < 2
        or not current.header_row
        or label is None
        or not label.text.strip()
        or any(column in first and first[column].text.strip() for column in columns if column)
        or previous_last_label is None
        or len(_split_lines(previous_last_label.text)) < 2
    ):
        return current
    header = _row_cells(previous, 0)
    if not any(column in header and header[column].text.strip() for column in columns if column):
        return current
    carried = [
        TableCell(
            text=header[column].text if column in header else "",
            row=0,
            column=column,
            confidence=header[column].confidence if column in header else 1,
        )
        for column in columns
    ]
    # The spill row (old row 0) is replaced by the header, so the data rows
    # keep their existing indices and the row count is unchanged.
    rest = [cell for cell in current.cells if cell.row >= 1]
    return current.model_copy(
        update={"cells": carried + rest, "rows": current.rows, "header_row": True}
    )


class TextLayerDocumentReader:
    """Reads text blocks and detected tables from the PDF text layer.

    It does no OCR: a page without a text layer (a pure scan) yields no
    evidence.
    """

    name = "pymupdf_text_layer"

    def read(self, pdf: Path, document_id: str) -> DocumentEvidence:
        # PyMuPDF advertises the optional pymupdf_layout package by PRINTING
        # a recommendation on first table detection — it is not routed through
        # the warnings machinery, so a warnings filter cannot suppress it
        # without hiding real warnings. The library's own opt-out switch
        # disables exactly that one message and nothing else.
        pymupdf.no_recommend_layout()
        content = pdf.read_bytes()
        pages: list[PageContent] = []
        previous_page_tables: list[TableStructure] = []
        with pymupdf.open(pdf) as document:
            for page_number, page in enumerate(document):
                # One element per printed line, not per block: a line carries
                # its own bounding box, which is the granularity a text-label
                # locator needs to pair a label with the value printed beside
                # it. Evidence written by older readers holds block-level
                # elements; the extractor still loads those and splits their
                # text into lines itself.
                elements: list[TextElement] = []
                for block in page.get_text("dict")["blocks"]:
                    for line in block.get("lines", []):
                        text = "".join(span["text"] for span in line["spans"]).strip()
                        if not text:
                            continue
                        x0, y0, x1, y1 = line["bbox"]
                        elements.append(
                            TextElement(
                                text=text,
                                confidence=1,
                                element_type="line",
                                bbox=BoundingBox(
                                    x=max(0, x0),
                                    y=max(0, y0),
                                    width=max(0, x1 - x0),
                                    height=max(0, y1 - y0),
                                    page=page_number,
                                ),
                            )
                        )
                tables: list[TableStructure] = []
                # use_layout is PyMuPDF's layout-based detector (the default
                # since 1.28); pin it so a PyMuPDF upgrade cannot silently
                # change detection behaviour. Table detection can fail on
                # malformed pages; fall back to text-only evidence and keep
                # reading.
                layout_detected = True
                try:
                    found_tables = page.find_tables(use_layout=True).tables
                except (AttributeError, ValueError):
                    found_tables = []
                if not found_tables:
                    # Borderless statistical tables defeat the layout
                    # detector; fall back to whitespace alignment for those
                    # pages only, so ruled tables keep the layout detector.
                    layout_detected = False
                    try:
                        found_tables = page.find_tables(
                            vertical_strategy="text", horizontal_strategy="text"
                        ).tables
                    except (AttributeError, ValueError):
                        found_tables = []
                # Rotated, borderless releases fragment under both detectors:
                # word spacing inside labels is misread as column gaps, so the
                # cells are torn words. When that happens, replace the torn
                # grids with a word-geometry rebuild.
                rebuilt_tables = rebuild_fragmented_tables(found_tables, page)
                if rebuilt_tables is not None:
                    tables.extend(
                        table.model_copy(update={"bordered": False}) for table in rebuilt_tables
                    )
                    found_tables = []
                for found in found_tables:
                    matrix = found.extract()
                    # `extract()` walks the same row and cell lists the
                    # `rows` property exposes, so zipping the two stamps each
                    # text cell with its own rectangle without touching text
                    # or row/column order; a spanned position is None in both
                    # and keeps no box. The strict zips hold only while that
                    # alignment does, which is the pinned detector behaviour.
                    row_rects = [row.cells for row in found.rows]
                    cells: list[TableCell] = []
                    for row_index, (texts, rects) in enumerate(zip(matrix, row_rects, strict=True)):
                        for column_index, (value, rect) in enumerate(
                            zip(texts, rects, strict=True)
                        ):
                            cells.append(
                                TableCell(
                                    text="" if value is None else str(value),
                                    row=row_index,
                                    column=column_index,
                                    confidence=1,
                                    bbox=_bbox_from_rect(rect, page_number),
                                )
                            )
                    tables.append(
                        _expand_grouped_rows(
                            TableStructure(
                                rows=len(matrix),
                                columns=max((len(row) for row in matrix), default=0),
                                cells=cells,
                                header_row=bool(matrix),
                                bbox=_bbox_from_rect(found.bbox, page_number),
                                # Only the layout detector reads ruling lines;
                                # the text fallback guesses from whitespace, so
                                # its "tables" are not geometrically certain
                                # and must never be treated as such by prose
                                # consumers (span exclusion).
                                bordered=layout_detected,
                            )
                        )
                    )
                if usable_label_pairs(tables) == 0:
                    # A bordered form whose rules live in a raster image
                    # defeats both detectors: no row a table-label locator
                    # could use survives. Rebuild the form's aligned
                    # label/value lines as a table instead.
                    form_table = build_form_table(page, page_number)
                    if form_table is not None:
                        tables.append(form_table.model_copy(update={"bordered": False}))
                if tables and previous_page_tables:
                    # A table that continues from the previous page opens with
                    # the spilled tail of the last row's label instead of a
                    # header; borrow the header so its columns keep context.
                    tables[0] = _carry_split_header(previous_page_tables[-1], tables[0])
                previous_page_tables = tables
                pages.append(
                    PageContent(
                        page_number=page_number,
                        width=page.rect.width,
                        height=page.rect.height,
                        text_elements=elements,
                        full_text=page.get_text(),
                        tables=tables,
                        confidence=1,
                    )
                )
        return DocumentEvidence(
            document_id=document_id,
            document_hash=hashlib.sha256(content).hexdigest(),
            reader_name=self.name,
            pages=pages,
        )
