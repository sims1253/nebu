from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pymupdf
import pytest

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource

if TYPE_CHECKING:
    from pathlib import Path

    from janus.comparison.content import TableStructure

X0, COL1, X1 = 48.0, 190.0, 360.0
TOP, ROW_HEIGHT = 80.0, 22.0
GRID = [
    ["Metric", "Value"],
    ["Nominal power", "12.5"],
    ["Peak power", "18.0"],
]


def ruled_table_pdf() -> bytes:
    """A ruled two-column table: one bordered cell per grid position."""
    document = pymupdf.open()
    page = document.new_page(width=420, height=300)
    page.insert_text((48, 52), "Device summary", fontsize=14)
    bottom = TOP + ROW_HEIGHT * len(GRID)
    for index in range(len(GRID) + 1):
        y = TOP + index * ROW_HEIGHT
        page.draw_line(pymupdf.Point(X0, y), pymupdf.Point(X1, y), color=(0.6, 0.6, 0.6), width=0.7)
    for x in (X0, COL1, X1):
        page.draw_line(
            pymupdf.Point(x, TOP), pymupdf.Point(x, bottom), color=(0.6, 0.6, 0.6), width=0.7
        )
    for row_index, row in enumerate(GRID):
        for column_index, text in enumerate(row):
            x = X0 + 8 if column_index == 0 else COL1 + 8
            page.insert_text((x, TOP + row_index * ROW_HEIGHT + 15), text, fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def grouped_table_pdf() -> bytes:
    """A ruled table whose second row groups three sub-entries under one border."""
    document = pymupdf.open()
    page = document.new_page(width=420, height=300)
    page.insert_text((48, 52), "Device summary", fontsize=14)
    group_top = TOP + ROW_HEIGHT
    group_bottom = group_top + 3 * ROW_HEIGHT
    # Header row.
    page.draw_rect(pymupdf.Rect(X0, TOP, X1, group_top), color=(0.6, 0.6, 0.6), width=0.7)
    page.draw_line(
        pymupdf.Point(COL1, TOP), pymupdf.Point(COL1, group_top), color=(0.6, 0.6, 0.6), width=0.7
    )
    page.insert_text((X0 + 8, TOP + 15), "Metric", fontsize=10)
    page.insert_text((COL1 + 8, TOP + 15), "Value", fontsize=10)
    # Grouped row: one cell region on each side, no inner separators.
    page.draw_rect(pymupdf.Rect(X0, group_top, X1, group_bottom), color=(0.6, 0.6, 0.6), width=0.7)
    page.draw_line(
        pymupdf.Point(COL1, group_top),
        pymupdf.Point(COL1, group_bottom),
        color=(0.6, 0.6, 0.6),
        width=0.7,
    )
    for offset, text in enumerate(("Power", "Nominal", "Peak")):
        page.insert_text((X0 + 8, group_top + 15 + offset * ROW_HEIGHT), text, fontsize=10)
    page.insert_text((COL1 + 8, group_top + 15 + ROW_HEIGHT), "12.5", fontsize=10)
    page.insert_text((COL1 + 8, group_top + 15 + 2 * ROW_HEIGHT), "18.0", fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def read_first_table(tmp_path: Path, content: bytes) -> TableStructure:
    pdf = tmp_path / "device.pdf"
    pdf.write_bytes(content)
    evidence = TextLayerDocumentReader().read(pdf, "device")
    assert len(evidence.pages[0].tables) == 1
    return evidence.pages[0].tables[0]


def test_ruled_table_cells_carry_boxes(tmp_path: Path) -> None:
    table = read_first_table(tmp_path, ruled_table_pdf())
    assert [(cell.row, cell.column, cell.text) for cell in table.cells] == [
        (row, column, GRID[row][column]) for row in range(len(GRID)) for column in (0, 1)
    ]
    for cell in table.cells:
        assert cell.bbox is not None
        assert cell.bbox.page == 0


def test_cell_boxes_match_the_drawn_grid(tmp_path: Path) -> None:
    table = read_first_table(tmp_path, ruled_table_pdf())
    cells = {(cell.row, cell.column): cell for cell in table.cells}
    for row in range(len(GRID)):
        for column, left in ((0, X0), (1, COL1)):
            box = cells[(row, column)].bbox
            assert box is not None
            assert box.x == pytest.approx(left, abs=0.5)
            assert box.y == pytest.approx(TOP + row * ROW_HEIGHT, abs=0.5)
            assert box.width == pytest.approx((COL1 if column == 0 else X1) - left, abs=0.5)
            assert box.height == pytest.approx(ROW_HEIGHT, abs=0.5)


def test_table_box_is_the_union_of_cell_boxes(tmp_path: Path) -> None:
    table = read_first_table(tmp_path, ruled_table_pdf())
    assert table.bbox is not None
    assert table.bbox.page == 0
    boxes = [cell.bbox for cell in table.cells if cell.bbox is not None]
    assert table.bbox.x == pytest.approx(min(box.x for box in boxes), abs=0.01)
    assert table.bbox.y == pytest.approx(min(box.y for box in boxes), abs=0.01)
    assert table.bbox.x + table.bbox.width == pytest.approx(
        max(box.x + box.width for box in boxes), abs=0.01
    )
    assert table.bbox.y + table.bbox.height == pytest.approx(
        max(box.y + box.height for box in boxes), abs=0.01
    )


def test_split_grouped_cells_inherit_the_parent_box(tmp_path: Path) -> None:
    table = read_first_table(tmp_path, grouped_table_pdf())
    cells = {(cell.row, cell.column): cell for cell in table.cells}
    # The grouped row splits into "Nominal" and "Peak" sub-rows; every split
    # cell keeps the geometry of the merged cell it was read from.
    group_top = TOP + ROW_HEIGHT
    for row in (1, 2):
        for column, left, right in ((0, X0, COL1), (1, COL1, X1)):
            box = cells[(row, column)].bbox
            assert box is not None
            assert box.x == pytest.approx(left, abs=0.5)
            assert box.y == pytest.approx(group_top, abs=0.5)
            assert box.width == pytest.approx(right - left, abs=0.5)
            assert box.height == pytest.approx(3 * ROW_HEIGHT, abs=0.5)
    assert cells[(1, 0)].bbox == cells[(2, 0)].bbox
    assert cells[(1, 1)].bbox == cells[(2, 1)].bbox


def test_extraction_reports_cell_and_table_boxes(tmp_path: Path) -> None:
    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Device summary check",
            "sections": [
                {
                    "key": "power",
                    "label": "Power",
                    "fields": [
                        {
                            "key": "peak",
                            "label": "Peak power",
                            "reference": {"pointer": "/power/peak"},
                            "value": {"type": "decimal"},
                            "locate": {
                                "strategy": "table_label",
                                "labels": ["Peak power"],
                            },
                            "compare": {"operator": "numeric", "absolute_tolerance": 0},
                        },
                    ],
                }
            ],
        }
    ).encode()
    reference = json.dumps({"power": {"peak": 18.0}}).encode()

    document_path = tmp_path / "device.pdf"
    document_path.write_bytes(ruled_table_pdf())

    plan = SpecificationCompiler().compile(
        specification, ReferenceSource(reference, "reference.json")
    )
    evidence = TextLayerDocumentReader().read(document_path, "device")
    extraction = SchemaExtractor().extract(plan, evidence)
    [field] = extraction.fields
    assert field.location is not None
    assert field.location.cell_bbox == {
        "x": pytest.approx(COL1),
        "y": pytest.approx(TOP + 2 * ROW_HEIGHT),
        "width": pytest.approx(X1 - COL1),
        "height": pytest.approx(ROW_HEIGHT),
    }
    assert field.location.table_bbox == {
        "x": pytest.approx(X0),
        "y": pytest.approx(TOP),
        "width": pytest.approx(X1 - X0),
        "height": pytest.approx(ROW_HEIGHT * len(GRID)),
    }
