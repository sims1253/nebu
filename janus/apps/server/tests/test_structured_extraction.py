"""The public extraction interface works independently of comparison or persistence."""

import json
import subprocess

import pymupdf
import pytest

from janus.comparison.content import BoundingBox, PageContent, TableCell, TableStructure
from janus.comparison.models import DocumentEvidence
from janus.extraction import CheckRule, ExtractionSpecification, compare, extract
from janus.extraction.engine import extract_evidence
from janus.extraction.models import Scalar
from janus.extraction.parsing import parse_value


def test_nested_data_and_comparison_need_no_review_or_reference_for_extraction(tmp_path):
    pdf = tmp_path / "order.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Order number: PO-1042")
        page.insert_text((72, 100), "Total: 1,275.50")
        document.save(pdf)
    spec = ExtractionSpecification.model_validate(
        {
            "name": "Order",
            "fields": {
                "order": {
                    "type": "object",
                    "fields": {
                        "id": {
                            "type": "text",
                            "locate": {"strategy": "text_label", "labels": ["Order number"]},
                        },
                        "total": {
                            "type": "decimal",
                            "group_separator": ",",
                            "locate": {"strategy": "text_label", "labels": ["Total"]},
                        },
                    },
                },
            },
        }
    )
    result = extract(pdf, spec)
    assert result.data == {"order": {"id": "PO-1042", "total": "1275.50"}}
    assert all(f.status == "extracted" and f.source is not None for f in result.fields)
    # Independent reference disagrees: detecting that is a successful comparison.
    checks = compare(
        result, {"total": 1000}, [CheckRule(path="/order/total", reference_pointer="/total")]
    )
    assert checks[0].status == "mismatch"
    checks = compare(
        result, {"total": 1275.5}, [CheckRule(path="/order/total", reference_pointer="/total")]
    )
    assert checks[0].status == "match"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(spec.model_dump_json())
    cli = subprocess.run(
        ["janus", "extract", str(pdf), str(spec_path), "--data-only"],
        capture_output=True,
        text=True,
    )
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout) == result.data


@pytest.mark.parametrize(
    ("text", "options", "expected"),
    [
        ("1,275.50", {"group_separator": ","}, "1275.50"),
        ("1.275,50", {"decimal_separator": ",", "group_separator": "."}, "1275.50"),
        ("1234", {}, "1234"),
        ("12.5%", {"type": "percentage"}, "12.5"),
        ("$12.50", {"strip_prefix": "$"}, "12.50"),
        ("12 (34%)", {"source_shape": "count_percentage", "component": "percentage"}, "34"),
    ],
)
def test_declared_number_formats(text, options, expected):
    assert parse_value(text, Scalar.model_validate({"type": "decimal", **options})) == expected


@pytest.mark.parametrize("text", ["1,234", "NaN", "Infinity", "1,23.00", "12.3.4"])
def test_unknown_or_invalid_numeric_formats_are_rejected(text):
    with pytest.raises(ValueError):
        parse_value(text, Scalar(type="decimal"))


def table_evidence():
    # Amount precedes description; missing cells must not shift neighboring records.
    rows = [["Amount", "Description", "Count"], ["12.50", "A", "2"], ["bad", "B", ""]]
    cells = [
        TableCell(
            text=text,
            row=r,
            column=c,
            bbox=BoundingBox(x=c * 100, y=r * 20, width=90, height=18, page=0),
        )
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
    ]
    return DocumentEvidence(
        document_id="table",
        document_hash="hash",
        reader_name="test",
        pages=[
            PageContent(
                page_number=0,
                width=500,
                height=700,
                tables=[TableStructure(rows=3, columns=3, cells=cells, header_row=True)],
            )
        ],
    )


def record_spec(max_rows=200):
    return ExtractionSpecification.model_validate(
        {
            "name": "Lines",
            "fields": {
                "lines": {
                    "type": "records",
                    "max_rows": max_rows,
                    "columns": {
                        "description": {"type": "text", "labels": ["Description"]},
                        "amount": {"type": "decimal", "labels": ["Amount"]},
                        "quantity": {"type": "integer", "labels": ["Count"]},
                    },
                }
            },
        }
    )


def test_records_follow_physical_rows_and_keep_invalid_and_missing_cells():
    result = extract_evidence(table_evidence(), record_spec())
    assert result.data == {
        "lines": [
            {"description": "A", "amount": "12.50", "quantity": 2},
            {"description": "B", "amount": None, "quantity": None},
        ]
    }
    fields = {f.path: f for f in result.fields}
    assert fields["/lines/1/amount"].status == "invalid"
    assert fields["/lines/1/quantity"].status == "missing"
    assert fields["/lines/0/amount"].source.row == 1
    assert fields["/lines/0/description"].source.row == 1
    assert fields["/lines/0/amount"].source.column == 0
    assert (
        compare(
            result, {"value": 0}, [CheckRule(path="/lines/1/amount", reference_pointer="/value")]
        )[0].status
        == "not_compared"
    )


def test_record_limit_is_reported_instead_of_silent_truncation():
    result = extract_evidence(table_evidence(), record_spec(max_rows=1))
    assert len(result.data["lines"]) == 1
    assert any(f.path == "/lines" and f.status == "error" for f in result.fields)


def test_empty_document_reports_missing_fields_and_tables():
    evidence = DocumentEvidence(
        document_id="empty", document_hash="hash", reader_name="test", pages=[]
    )
    result = extract_evidence(evidence, record_spec())
    assert result.data == {"lines": []}
    assert result.fields[0].status == "missing"


def test_pointer_keys_are_escaped():
    spec = ExtractionSpecification.model_validate(
        {
            "name": "Escaping",
            "fields": {
                "a/b~c": {
                    "type": "text",
                    "locate": {"strategy": "text_label", "labels": ["Missing"]},
                }
            },
        }
    )
    evidence = DocumentEvidence(
        document_id="empty", document_hash="hash", reader_name="test", pages=[]
    )
    result = extract_evidence(evidence, spec)
    assert result.fields[0].path == "/a~1b~0c"
    assert result.data == {"a/b~c": None}


def test_schema_is_serializable():
    assert (
        json.loads(json.dumps(ExtractionSpecification.model_json_schema()))["title"]
        == "ExtractionSpecification"
    )


@pytest.mark.parametrize(
    "headers",
    [
        ["Description", "Count", "Amount"],
        ["Amount", "Description", "Count"],
    ],
)
def test_same_record_spec_extracts_real_pdfs_with_reordered_columns(tmp_path, headers):
    pdf = tmp_path / "lines.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        for x in [50, 200, 350, 500]:
            page.draw_line((x, 50), (x, 170))
        for y in [50, 90, 130, 170]:
            page.draw_line((50, y), (500, y))
        rows = [
            dict(zip(headers, headers, strict=True)),
            {"Description": "Paper", "Count": "2", "Amount": "12.50"},
            {"Description": "Pens", "Count": "3", "Amount": "7.25"},
        ]
        for r, row in enumerate(rows):
            for c, header in enumerate(headers):
                page.insert_text((60 + c * 150, 75 + r * 40), row[header])
        document.save(pdf)
    result = extract(pdf, record_spec())
    assert result.data["lines"] == [
        {"description": "Paper", "quantity": 2, "amount": "12.50"},
        {"description": "Pens", "quantity": 3, "amount": "7.25"},
    ]
    assert all(field.status == "extracted" for field in result.fields)


def result_with(value, value_type):
    from janus.extraction.models import FieldResult, StructuredExtraction

    return StructuredExtraction(
        specification_name="Test",
        specification_hash="spec",
        document_hash="doc",
        data={"value": value},
        fields=[FieldResult(path="/value", type=value_type, status="extracted")],
        pages=[],
    )


def test_xlsx_dates_compare_calendar_days(tmp_path):
    from datetime import datetime

    from openpyxl import Workbook

    from janus.comparison.reference import ReferenceSource, normalize_reference

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["date"])
    sheet.append([datetime(2024, 3, 1, 14, 30)])
    path = tmp_path / "reference.xlsx"
    workbook.save(path)
    reference, _ = normalize_reference(ReferenceSource.from_path(path))
    rule = CheckRule(path="/value", reference_pointer="/sheets/Sheet/rows/0/date")
    assert compare(result_with("2024-03-01", "date"), reference, [rule])[0].status == "match"
    assert compare(result_with("2024-03-02", "date"), reference, [rule])[0].status == "mismatch"


def test_csv_booleans_compare_as_boolean_values():
    from janus.comparison.reference import ReferenceSource, normalize_reference

    reference, _ = normalize_reference(ReferenceSource(b"flag\nyes\n", "reference.csv"))
    assert (
        compare(
            result_with(True, "boolean"),
            reference,
            [CheckRule(path="/value", reference_pointer="/rows/0/flag")],
        )[0].status
        == "match"
    )


def test_decimal_difference_does_not_round_into_tolerance():
    result = result_with("0.0100000000000000000000000000001", "decimal")
    assert (
        compare(
            result,
            {"value": 0},
            [CheckRule(path="/value", reference_pointer="/value", absolute_tolerance=0.01)],
        )[0].status
        == "mismatch"
    )


def test_comparison_requires_at_least_one_check():
    with pytest.raises(ValueError, match="At least one"):
        compare(result_with("A", "text"), {}, [])


def test_cli_reports_unreadable_pdf_without_traceback(tmp_path):
    document = tmp_path / "bad.pdf"
    document.write_bytes(b"not a PDF")
    spec = tmp_path / "spec.json"
    spec.write_text(record_spec().model_dump_json())
    cli = subprocess.run(
        ["janus", "extract", str(document), str(spec)], capture_output=True, text=True
    )
    assert cli.returncode == 1
    assert cli.stderr.startswith("janus:")
    assert "Traceback" not in cli.stderr
    assert cli.stdout == ""
