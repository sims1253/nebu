from __future__ import annotations

import json

import pymupdf

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.content import TableCell, TableStructure
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus, ExtractionStatus
from janus.comparison.reader import TextLayerDocumentReader, _expand_grouped_rows
from janus.comparison.reference import ReferenceSource


def table(cells: list[TableCell], *, rows: int, columns: int) -> TableStructure:
    return TableStructure(rows=rows, columns=columns, cells=cells, header_row=True)


def test_grouped_row_splits_with_section_stamp() -> None:
    structure = table(
        [
            TableCell(text="Metric", row=0, column=0),
            TableCell(text="Value", row=0, column=1),
            TableCell(text="Power\nNominal\nPeak", row=1, column=0),
            TableCell(text="12.5\n18.0", row=1, column=1),
            TableCell(text="Serial", row=2, column=0),
            TableCell(text="SN-778", row=2, column=1),
        ],
        rows=3,
        columns=2,
    )
    expanded = _expand_grouped_rows(structure)
    grid = {(cell.row, cell.column): cell for cell in expanded.cells}
    assert expanded.rows == 4
    assert grid[(1, 0)].text == "Nominal"
    assert grid[(1, 0)].section == "Power"
    assert grid[(1, 1)].text == "12.5"
    assert grid[(2, 0)].text == "Peak"
    assert grid[(2, 1)].text == "18.0"
    assert grid[(3, 0)].text == "Serial"
    assert grid[(3, 0)].section is None


def test_grouped_row_without_extra_group_line_splits_directly() -> None:
    structure = table(
        [
            TableCell(text="Metric", row=0, column=0),
            TableCell(text="Value", row=0, column=1),
            TableCell(text="Min\nMax", row=1, column=0),
            TableCell(text="1\n9", row=1, column=1),
        ],
        rows=2,
        columns=2,
    )
    expanded = _expand_grouped_rows(structure)
    grid = {(cell.row, cell.column): cell for cell in expanded.cells}
    assert expanded.rows == 3
    assert grid[(1, 0)].text == "Min"
    assert grid[(1, 0)].section is None
    assert grid[(2, 1)].text == "9"


def test_wrapped_label_with_single_line_value_stays_one_row() -> None:
    structure = table(
        [
            TableCell(text="Metric", row=0, column=0),
            TableCell(text="Value", row=0, column=1),
            TableCell(text="Maximum power\nunder load", row=1, column=0),
            TableCell(text="18.0", row=1, column=1),
        ],
        rows=2,
        columns=2,
    )
    expanded = _expand_grouped_rows(structure)
    grid = {(cell.row, cell.column): cell for cell in expanded.cells}
    assert expanded.rows == 2
    assert grid[(1, 0)].text == "Maximum power\nunder load"
    assert grid[(1, 1)].text == "18.0"


def test_multi_line_header_row_stays_untouched() -> None:
    structure = table(
        [
            TableCell(text="Metric", row=0, column=0),
            TableCell(text="Measured\nvalue", row=0, column=1),
            TableCell(text="Serial", row=1, column=0),
            TableCell(text="SN-778", row=1, column=1),
        ],
        rows=2,
        columns=2,
    )
    expanded = _expand_grouped_rows(structure)
    grid = {(cell.row, cell.column): cell for cell in expanded.cells}
    assert grid[(0, 1)].text == "Measured\nvalue"


def grouped_table_pdf() -> bytes:
    """A device summary whose spec table groups sub-metrics under one border."""
    document = pymupdf.open()
    page = document.new_page(width=420, height=300)
    page.insert_text((48, 52), "Device summary", fontsize=14)

    x0, col1, x1 = 48, 190, 360
    top = 80
    row_height = 22
    group_lines = 3

    # Header row.
    page.draw_rect(pymupdf.Rect(x0, top, x1, top + row_height), color=(0.6, 0.6, 0.6), width=0.7)
    page.draw_line(
        pymupdf.Point(col1, top),
        pymupdf.Point(col1, top + row_height),
        color=(0.6, 0.6, 0.6),
        width=0.7,
    )
    page.insert_text((x0 + 8, top + 15), "Metric", fontsize=10)
    page.insert_text((col1 + 8, top + 15), "Value", fontsize=10)

    # Grouped row: one cell region holding "Power / Nominal / Peak" against
    # "12.5 / 18.0" — no separators between the sub-rows.
    group_top = top + row_height
    group_height = group_lines * row_height
    page.draw_rect(
        pymupdf.Rect(x0, group_top, x1, group_top + group_height), color=(0.6, 0.6, 0.6), width=0.7
    )
    page.draw_line(
        pymupdf.Point(col1, group_top),
        pymupdf.Point(col1, group_top + group_height),
        color=(0.6, 0.6, 0.6),
        width=0.7,
    )
    page.insert_text((x0 + 8, group_top + 15), "Power", fontsize=10)
    page.insert_text((x0 + 8, group_top + 15 + row_height), "Nominal", fontsize=10)
    page.insert_text((x0 + 8, group_top + 15 + 2 * row_height), "Peak", fontsize=10)
    page.insert_text((col1 + 8, group_top + 15 + row_height), "12.5", fontsize=10)
    page.insert_text((col1 + 8, group_top + 15 + 2 * row_height), "18.0", fontsize=10)

    content = document.tobytes()
    document.close()
    return content


def test_grouped_pdf_rows_extract_and_match(tmp_path) -> None:
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
                            "key": "nominal",
                            "label": "Nominal power",
                            "reference": {"pointer": "/power/nominal"},
                            "value": {"type": "decimal"},
                            "locate": {
                                "strategy": "table_label",
                                "labels": ["Nominal"],
                                "section_labels": ["Power"],
                            },
                            "compare": {"operator": "numeric", "absolute_tolerance": 0},
                        },
                        {
                            "key": "peak",
                            "label": "Peak power",
                            "reference": {"pointer": "/power/peak"},
                            "value": {"type": "decimal"},
                            "locate": {
                                "strategy": "table_label",
                                "labels": ["Peak"],
                                "section_labels": ["Power"],
                            },
                            "compare": {"operator": "numeric", "absolute_tolerance": 0},
                        },
                    ],
                }
            ],
        }
    ).encode()
    reference = json.dumps({"power": {"nominal": 12.5, "peak": 18.0}}).encode()

    document_path = tmp_path / "device.pdf"
    document_path.write_bytes(grouped_table_pdf())

    plan = SpecificationCompiler().compile(
        specification, ReferenceSource(reference, "reference.json")
    )
    evidence = TextLayerDocumentReader().read(document_path, "device")
    extraction = SchemaExtractor().extract(plan, evidence)
    assert all(field.status is ExtractionStatus.EXTRACTED for field in extraction.fields)
    comparisons = ComparisonEngine().compare(plan, extraction)
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)
