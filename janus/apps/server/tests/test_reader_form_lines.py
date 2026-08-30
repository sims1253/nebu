from __future__ import annotations

import json

import pymupdf

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.form_lines import build_form_table, usable_label_pairs
from janus.comparison.models import ComparisonStatus
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource


def form_over_raster_image() -> bytes:
    """A bordered form whose rules live in a raster image, plus a prose
    column, and marker boxes: the layout detector only recovers the markers
    and no detector yields a label/value row."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=420)
    background = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 300, 420), False)
    background.clear_with(255)
    page.insert_image(pymupdf.Rect(40, 80, 310, 400), pixmap=background)
    page.insert_text((60, 110), "Account summary", fontsize=13)
    rows = [
        ("Current due", "$1,284.46"),
        ("Late charge", "$0.00"),
        ("Previous balance", "$702.52"),
        ("Total due", "$1,986.98"),
        ("Paid this period", "$0.00"),
    ]
    for index, (label, value) in enumerate(rows):
        y = 140 + index * 26
        page.insert_text((60, y), label, fontsize=9)
        page.insert_text((200, y), value, fontsize=9)
    notes = [
        "Current due: the amount payable for the current",
        "charging period, before any earlier balance.",
        "Total due: everything owed, including balances",
        "carried over from earlier charging periods.",
        "Paid this period: payments received to date.",
    ]
    for index, line in enumerate(notes):
        page.insert_text((340, 140 + index * 16), line, fontsize=9)
    for x, y, marker in ((206, 132, "1"), (206, 184, "2")):
        page.draw_rect(pymupdf.Rect(x, y, x + 12, y + 12), color=(0.2, 0.2, 0.2), width=0.7)
        page.draw_line(
            pymupdf.Point(x + 6, y), pymupdf.Point(x + 6, y + 12), color=(0.2, 0.2, 0.2), width=0.7
        )
        page.insert_text((x + 2, y + 9), marker, fontsize=7)
    content = document.tobytes()
    document.close()
    return content


def ruled_table() -> bytes:
    """A plain ruled table: the layout detector already yields label/value
    rows, so the aligned-span fallback must stay out of the way."""
    document = pymupdf.open()
    page = document.new_page(width=420, height=240)
    page.draw_rect(pymupdf.Rect(48, 72, 372, 96), color=(0.6, 0.6, 0.6), width=0.7)
    page.draw_line(pymupdf.Point(240, 72), pymupdf.Point(240, 96), color=(0.6, 0.6, 0.6), width=0.7)
    page.insert_text((56, 88), "Position", fontsize=10)
    page.insert_text((248, 88), "Amount", fontsize=10)
    rows = [("Net amount", "1,840.00"), ("Grand total", "2,189.60")]
    for index, (label, value) in enumerate(rows):
        top = 96 + index * 24
        page.draw_rect(pymupdf.Rect(48, top, 372, top + 24), color=(0.6, 0.6, 0.6), width=0.7)
        page.draw_line(
            pymupdf.Point(240, top), pymupdf.Point(240, top + 24), color=(0.6, 0.6, 0.6), width=0.7
        )
        page.insert_text((56, top + 16), label, fontsize=10)
        page.insert_text((248, top + 16), value, fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def test_form_over_raster_image_extracts_and_matches(tmp_path) -> None:
    fields = []
    pointers = {
        "current_due": "/summary/current_due",
        "late_charge": "/summary/late_charge",
        "previous_balance": "/summary/previous_balance",
        "total_due": "/summary/total_due",
        "paid": "/summary/paid",
    }
    labels = {
        "current_due": "Current due",
        "late_charge": "Late charge",
        "previous_balance": "Previous balance",
        "total_due": "Total due",
        "paid": "Paid this period",
    }
    for key, pointer in pointers.items():
        fields.append(
            {
                "key": key,
                "label": labels[key],
                "reference": {"pointer": pointer},
                "value": {"type": "decimal"},
                "locate": {"strategy": "table_label", "labels": [labels[key]]},
                "compare": {
                    "operator": "exact",
                    "normalizers": ["trim", "casefold", "numeric_punctuation"],
                },
            }
        )
    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Account summary check",
            "sections": [{"key": "summary", "label": "Summary", "fields": fields}],
        }
    ).encode()
    reference = json.dumps(
        {
            "summary": {
                "current_due": "$1,284.46",
                "late_charge": "$0.00",
                "previous_balance": "$702.52",
                "total_due": "$1,986.98",
                "paid": "$0.00",
            }
        }
    ).encode()

    document_path = tmp_path / "form.pdf"
    document_path.write_bytes(form_over_raster_image())
    plan = SpecificationCompiler().compile(
        specification, ReferenceSource(reference, "reference.json")
    )
    evidence = TextLayerDocumentReader().read(document_path, "form")
    form_tables = [
        table for table in evidence.pages[0].tables if table.columns == 2 and not table.header_row
    ]
    assert form_tables, "the aligned label/value fallback should rebuild the form"
    rebuilt = {(cell.row, cell.column): cell.text for cell in form_tables[0].cells}
    for row in range(form_tables[0].rows):
        assert rebuilt[(row, 1)].startswith("$"), "every rebuilt row pairs a label with a value"
    comparisons = ComparisonEngine().compare(plan, SchemaExtractor().extract(plan, evidence))
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_ruled_table_keeps_the_detector_result(tmp_path) -> None:
    document_path = tmp_path / "table.pdf"
    document_path.write_bytes(ruled_table())
    evidence = TextLayerDocumentReader().read(document_path, "table")
    assert usable_label_pairs(evidence.pages[0].tables) >= 2
    assert all(table.header_row or table.columns != 2 for table in evidence.pages[0].tables), (
        "a page the detector reads well must not gain a rebuilt form table"
    )


def test_prose_page_is_not_a_form(tmp_path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=420, height=240)
    page.insert_text((48, 96), "Quarterly notes", fontsize=13)
    for index, line in enumerate(
        ["Amounts are reconciled each quarter.", "One open item remains unpaid."]
    ):
        page.insert_text((48, 130 + index * 18), line, fontsize=10)
    content = document.tobytes()
    document.close()
    document_path = tmp_path / "notes.pdf"
    document_path.write_bytes(content)
    evidence = TextLayerDocumentReader().read(document_path, "notes")
    assert evidence.pages[0].tables == []
    with pymupdf.open(document_path) as reopened:
        assert build_form_table(reopened[0], 0) is None
