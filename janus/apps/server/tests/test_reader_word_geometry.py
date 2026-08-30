from __future__ import annotations

import json

import pymupdf

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource

# Columns, in visual left-to-right order: their distance from the visual
# right edge (the page is drawn rotated a quarter turn, so a column's visual
# x position is the insertion y in the unrotated coordinate space).
_COLUMNS = [300, 350, 400, 450, 500]
_HEADERS = ["2022", "2023", "2024", "Q1r", "Q2p"]
_BLOCKS = [
    (
        "Total consumption",
        [
            ("Hydro", ["1.2", "1.3", "1.4", "0.3", "0.4"]),
            ("Wind", ["2.2", "2.3", "2.4", "0.6", "0.6"]),
        ],
        ["12.5", "13.0", "13.8", "3.4", "3.5"],
    ),
    (
        "Peak load",
        [("Hydro", ["0.9", "1.0", "1.1", "0.2", "0.3"])],
        ["9.9", "10.1", "10.4", "2.6", "2.7"],
    ),
]


def rotated_release_pdf() -> bytes:
    """A borderless statistical table drawn sideways, like a rotated release.

    Text advances down the unrotated page (reading order) while the page is
    displayed a quarter turn rotated, so the table's rows and columns swap
    axes in the raw coordinate space — the shape that tears the text-strategy
    detector into micro-columns.
    """
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((80, 732), "Regional electricity statistics", fontsize=13, rotate=90)
    page.insert_text((104, 732), "Gigawatt-hours, seasonally adjusted", fontsize=8, rotate=90)
    for x, header in zip(_COLUMNS, _HEADERS, strict=True):
        page.insert_text((120, 792 - x), header, fontsize=9, rotate=90)
    row_y = 140
    for label, children, values in _BLOCKS:
        page.insert_text((row_y, 712), label, fontsize=9, rotate=90)
        for x, value in zip(_COLUMNS, values, strict=True):
            page.insert_text((row_y, 792 - x), value, fontsize=9, rotate=90)
        row_y += 16
        for child_label, child_values in children:
            page.insert_text((row_y, 697), child_label, fontsize=9, rotate=90)
            for x, value in zip(_COLUMNS, child_values, strict=True):
                page.insert_text((row_y, 792 - x), value, fontsize=9, rotate=90)
            row_y += 16
        row_y += 10
    page.set_rotation(90)
    content = document.tobytes()
    document.close()
    return content


def test_rotated_borderless_table_is_rebuilt_from_word_geometry(tmp_path) -> None:
    document_path = tmp_path / "release.pdf"
    document_path.write_bytes(rotated_release_pdf())

    evidence = TextLayerDocumentReader().read(document_path, "release")
    assert evidence.pages[0].tables, "the word-geometry rebuild should recover the table"
    table = evidence.pages[0].tables[0]
    cells = {(cell.row, cell.column): cell for cell in table.cells}
    # Column context comes from the period tokens above the first data row.
    assert [cells[(0, column)].text for column in range(1, table.columns)] == [
        "2022",
        "2023",
        "2024",
        "Q1r",
        "Q2p",
    ]
    # Summary rows head the rows nested under them; section context tells
    # the two equally labelled rows apart.
    labels = {cells[(row, 0)].text: cells[(row, 0)].section for row in range(1, table.rows)}
    assert labels["Total consumption"] is None
    assert labels["Hydro"] == "Peak load"  # nearest of the two summary rows
    assert cells[(1, 1)].text == "12.5"


def _field(
    key: str,
    label: str,
    pointer: str,
    labels: list[str],
    column: str,
    sections: list[str] | None = None,
) -> dict:
    locate = {"strategy": "table_label", "labels": labels, "column_labels": [column]}
    if sections is not None:
        locate["section_labels"] = sections
    return {
        "key": key,
        "label": label,
        "reference": {"pointer": pointer},
        "value": {"type": "decimal"},
        "locate": locate,
        "compare": {"operator": "numeric", "normalizers": ["numeric_punctuation"]},
    }


def test_rotated_release_fields_extract_and_match(tmp_path) -> None:
    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Regional electricity check",
            "sections": [
                {
                    "key": "grid",
                    "label": "Grid statistics",
                    "fields": [
                        _field(
                            "total_2022",
                            "Total consumption 2022",
                            "/grid/total_2022",
                            ["Total consumption"],
                            "2022",
                        ),
                        _field(
                            "total_q2",
                            "Total consumption latest quarter",
                            "/grid/total_q2",
                            ["Total consumption"],
                            "Q2p",
                        ),
                        _field(
                            "hydro_generation",
                            "Hydro generation 2022",
                            "/grid/hydro_generation",
                            ["Hydro"],
                            "2022",
                            ["Total consumption"],
                        ),
                        _field(
                            "hydro_peak",
                            "Hydro peak 2022",
                            "/grid/hydro_peak",
                            ["Hydro"],
                            "2022",
                            ["Peak load"],
                        ),
                    ],
                }
            ],
        }
    ).encode()
    reference = json.dumps(
        {
            "grid": {
                "total_2022": 12.5,
                "total_q2": 3.5,
                "hydro_generation": 1.2,
                "hydro_peak": 0.9,
            }
        }
    ).encode()

    document_path = tmp_path / "release.pdf"
    document_path.write_bytes(rotated_release_pdf())
    plan = SpecificationCompiler().compile(specification, ReferenceSource(reference, "r.json"))
    evidence = TextLayerDocumentReader().read(document_path, "release")
    comparisons = ComparisonEngine().compare(plan, SchemaExtractor().extract(plan, evidence))
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)
    by_key = {item.field_key: item for item in comparisons}
    # The equally labelled rows resolve through their section context.
    assert (
        by_key["hydro_generation"].document_location.row
        != by_key["hydro_peak"].document_location.row
    )
    assert by_key["hydro_generation"].row_context == "Total consumption"
    assert by_key["hydro_peak"].row_context == "Peak load"
    # The equally labelled columns resolve through their header context.
    assert by_key["total_2022"].column_context == "2022"
    assert by_key["total_q2"].column_context == "Q2p"
