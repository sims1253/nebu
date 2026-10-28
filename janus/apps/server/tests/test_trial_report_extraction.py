"""A clinical trial report as journals print it: spanned headers, per-arm N in header and
group rows, estimator labels, typographic minus signs, bare zero counts, not-reported marks,
and a label whose value is set as its own line. Driven through the public interface on a PDF
read by Janus's own reader."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pymupdf
import pytest

from janus.comparison.content import PageContent, TableCell, TableStructure
from janus.comparison.models import DocumentEvidence
from janus.comparison.reader import TextLayerDocumentReader
from janus.extraction import (
    ExtractionSpecification,
    extract,
    extract_evidence,
    specification_hash,
)
from janus.extraction.canonical import canonical_json

if TYPE_CHECKING:
    from pathlib import Path

FONT = pymupdf.Font("helv")
MINUS = "\N{MINUS SIGN}"
ROW_HEIGHT = 18


def ruled_table(page: pymupdf.Page, top: float, widths: list[float], rows: list[list]) -> float:
    """Draw a ruled table; None in a row continues the cell to its left (a spanned header).
    Text goes through a TextWriter so that U+2212 is printed as itself."""
    xs = [40.0]
    for width in widths:
        xs.append(xs[-1] + width)
    writer = pymupdf.TextWriter(page.rect)
    y = top
    page.draw_line((xs[0], y), (xs[-1], y))
    for row in rows:
        page.draw_line((xs[0], y), (xs[0], y + ROW_HEIGHT))
        for column, text in enumerate(row):
            if text:
                writer.append((xs[column] + 3, y + 13), text, font=FONT, fontsize=8)
            if column + 1 < len(row) and row[column + 1] is None:
                continue
            page.draw_line((xs[column + 1], y), (xs[column + 1], y + ROW_HEIGHT))
        y += ROW_HEIGHT
        page.draw_line((xs[0], y), (xs[-1], y))
    writer.write_text(page)
    return y


def trial_report(path: Path) -> Path:
    with pymupdf.open() as document:
        page = document.new_page(width=595, height=842)
        page.insert_text((40, 60), "Table 1 Baseline characteristics", fontsize=10)
        ruled_table(
            page,
            70,
            [130, 62, 62, 52, 62, 62, 52],
            [
                [
                    "Characteristic",
                    "Primary analysis set",
                    None,
                    None,
                    "All participants set",
                    None,
                    None,
                ],
                ["", "Control", "Intervention", "Total", "Control", "Intervention", "Total"],
                ["Female sex, n (%)", "20 (39.2)", "0", "20 (19.4)", "52 (39.4)", "7 (5.4)", "59"],
            ],
        )
        page.insert_text((40, 160), "Table 2 Outcomes", fontsize=10)
        ruled_table(
            page,
            170,
            [140, 90, 90, 150],
            [
                ["Outcome", "Vitamin C group", "Control group", "Effect size (95% CI)"],
                ["", "N = 355", "N = 352", ""],
                ["Primary analysis set", "N = 51", "N = 52", ""],
                ["Deaths, n (%)", "1 (2.0)", "0", "RR:1.124 (0.978, 1.290)"],
                ["All participants set", "N = 132", "N = 130", ""],
                ["Deaths, n (%)", "3 (2.3)", "NR", f"{MINUS}0.040 ({MINUS}0.187, 0.107)"],
            ],
        )
        # Two text columns; "Mean HbA1c:" ends the left one, its value is a line of the right.
        left = ["Follow-up lasted", "twelve months.", "Mean HbA1c:"]
        right = ["values improved in both", "arms of the trial.", "6.7"]
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            page.insert_text((60, 340 + index * 14), a, fontsize=10)
            page.insert_text((200, 340 + index * 14), b, fontsize=10)
        document.save(path)
    return path


def table(labels: list[str], **options: Any) -> dict[str, Any]:
    locate: dict[str, Any] = {"strategy": "table_label", "labels": labels}
    for key in ("column_labels", "section_labels"):
        if key in options:
            locate[key] = options.pop(key)
    return {"type": "integer", **options, "locate": locate}


def interval(component: str, section: str) -> dict[str, Any]:
    return table(
        ["Deaths, n (%)"],
        type="decimal",
        source_shape="estimate_interval",
        component=component,
        column_labels=["Effect size (95% CI)"],
        section_labels=[section],
    )


def count(arm: str, section: str, component: str = "count") -> dict[str, Any]:
    return table(
        ["Deaths, n (%)"],
        type="integer" if component == "count" else "percentage",
        source_shape="count_percentage",
        component=component,
        column_labels=[arm],
        section_labels=[section],
    )


SPECIFICATION = {
    "name": "Vitamin C trial",
    "fields": {
        "baseline": {
            "type": "object",
            "fields": {
                "female_primary_control": table(
                    ["Female sex, n (%)"],
                    source_shape="count_percentage",
                    component="count",
                    column_labels=["Primary analysis set Control"],
                ),
                "female_all_control": table(
                    ["Female sex, n (%)"],
                    source_shape="count_percentage",
                    component="count",
                    column_labels=["All participants set Control"],
                ),
                "female_primary_intervention": table(
                    ["Female sex, n (%)"],
                    source_shape="count_percentage",
                    component="count",
                    column_labels=["Primary analysis set Intervention"],
                ),
                "female_primary_intervention_percent": table(
                    ["Female sex, n (%)"],
                    type="percentage",
                    source_shape="count_percentage",
                    component="percentage",
                    column_labels=["Primary analysis set Intervention"],
                ),
            },
        },
        "randomized": {
            "type": "object",
            "fields": {
                "vitamin_c": table(["Vitamin C group"], column_labels=["Vitamin C group"]),
                "control": table(["Control group"], column_labels=["Control group"]),
            },
        },
        "deaths": {
            "type": "object",
            "fields": {
                "primary_vitamin_c": count("Vitamin C group", "Primary analysis set"),
                "all_vitamin_c": count("Vitamin C group", "All participants set"),
                "all_control": count("Control group", "All participants set"),
                "primary_rr": interval("estimate", "Primary analysis set"),
                "primary_rr_lower": interval("lower", "Primary analysis set"),
                "primary_rr_upper": interval("upper", "Primary analysis set"),
                "all_difference": interval("estimate", "All participants set"),
                "all_difference_lower": interval("lower", "All participants set"),
            },
        },
        "hba1c": {
            "type": "decimal",
            "locate": {"strategy": "text_label", "labels": ["Mean HbA1c"]},
        },
    },
}


def test_a_trial_report_extracts_what_a_reviewer_reads_in_its_tables(tmp_path):
    pdf = trial_report(tmp_path / "trial.pdf")
    # A caller with its own reader output uses the public extract_evidence.
    evidence = TextLayerDocumentReader().read(pdf, pdf.name, normalize_rotation=True)
    result = extract_evidence(evidence, ExtractionSpecification.model_validate(SPECIFICATION))
    fields = {field.path: field for field in result.fields}

    assert result.data == {
        "baseline": {
            # Column context is the whole header path, so the two "Control" columns differ.
            "female_primary_control": 20,
            "female_all_control": 52,
            # A zero count printed without its percentage is a count, and the percentage it
            # does not print is missing rather than invented.
            "female_primary_intervention": 0,
            "female_primary_intervention_percent": None,
        },
        # Per-arm N printed in a header row under the arm's name.
        "randomized": {"vitamin_c": 355, "control": 352},
        "deaths": {
            # "Primary analysis set | N = 51 | N = 52" heads the "Deaths" row below it.
            "primary_vitamin_c": 1,
            "all_vitamin_c": 3,
            # "NR" is not reported: missing, not a malformed value.
            "all_control": None,
            # "RR:1.124 (0.978, 1.290)": the estimator label is not part of the shape.
            "primary_rr": "1.124",
            "primary_rr_lower": "0.978",
            "primary_rr_upper": "1.290",
            # The difference prints the Unicode minus sign, U+2212.
            "all_difference": "-0.040",
            "all_difference_lower": "-0.187",
        },
        "hba1c": "6.7",
    }
    statuses = {path: field.status for path, field in fields.items()}
    assert statuses == {
        **dict.fromkeys(statuses, "extracted"),
        "/baseline/female_primary_intervention_percent": "missing",
        "/deaths/all_control": "missing",
    }
    percent = fields["/baseline/female_primary_intervention_percent"]
    assert percent.source is not None and percent.source.raw_text == "0"
    assert "carries no percentage" in percent.message
    not_reported = fields["/deaths/all_control"]
    assert not_reported.source is not None and not_reported.source.raw_text == "NR"
    assert "'NR'" in not_reported.message and "not reported" in not_reported.message

    # Locations name the cells that print the values.
    n = fields["/randomized/vitamin_c"].source
    assert n is not None and (n.row, n.column, n.raw_text) == (1, 1, "N = 355")
    deaths = fields["/deaths/all_vitamin_c"].source
    assert deaths is not None and (deaths.row, deaths.column) == (5, 1)
    # The text label's value sits on its own line beside the label: row names that line,
    # the one raw_text and cell_bbox describe.
    hba1c = fields["/hba1c"].source
    assert hba1c is not None and hba1c.raw_text == "6.7"
    lines = [element.text for element in evidence.pages[0].text_elements]
    assert lines[hba1c.row] == "6.7"
    assert hba1c.cell_bbox == evidence.pages[0].text_elements[hba1c.row].bbox.model_dump(
        exclude={"page"}
    )


def test_not_reported_marks_are_configurable(tmp_path):
    pdf = trial_report(tmp_path / "trial.pdf")
    field = count("Control group", "All participants set")
    spec = {"name": "Deaths", "fields": {"deaths": field}}
    default = extract(pdf, ExtractionSpecification.model_validate(spec))
    assert (default.data, default.fields[0].status) == ({"deaths": None}, "missing")
    # A specification that reads every value as printed sees a malformed count.
    spec["fields"]["deaths"] = {**field, "not_reported": []}
    strict = extract(pdf, ExtractionSpecification.model_validate(spec))
    assert (strict.data, strict.fields[0].status) == ({"deaths": None}, "invalid")


def test_the_same_document_and_specification_give_byte_identical_results(tmp_path):
    pdf = trial_report(tmp_path / "trial.pdf")

    # The same content with every object's members in another order.
    def reordered(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: reordered(value[key]) for key in reversed(list(value))}
        return value

    first = ExtractionSpecification.model_validate(SPECIFICATION)
    second = ExtractionSpecification.model_validate_json(json.dumps(reordered(SPECIFICATION)))
    assert first.model_dump_json() != second.model_dump_json()
    assert specification_hash(first) == specification_hash(second)

    when = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)
    runs = [extract(pdf, spec, created_at=when) for spec in (first, first, second)]
    assert runs[0].model_dump_json() == runs[1].model_dump_json()
    assert runs[0].created_at == when
    assert runs[2].specification_hash == runs[0].specification_hash
    reads = [
        TextLayerDocumentReader().read(pdf, pdf.name, created_at=when).model_dump_json()
        for _ in range(2)
    ]
    assert reads[0] == reads[1]

    # The hash is SHA-256 over RFC 8785 canonical JSON; the RFC's own example (section 3.2.2).
    example = json.loads(
        '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001],'
        ' "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
        ' "literals": [null, true, false]}'
    )
    assert (
        canonical_json(example)
        == (
            '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
            '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
        ).encode()
    )
    assert (
        specification_hash(first)
        == hashlib.sha256(canonical_json(first.model_dump(mode="json"))).hexdigest()
    )


def evidence_with(rows: list[list[str]], sections: list[str | None] | None = None):
    """One table as a reader hands it over: row 0 is the header row, "" a cell without text,
    `sections` the group the reader stamped on each row."""
    cells = [
        TableCell(text=text, row=r, column=c, section=sections[r] if sections else None)
        for r, row in enumerate(rows)
        for c, text in enumerate(row)
    ]
    table = TableStructure(rows=len(rows), columns=len(rows[0]), cells=cells, header_row=True)
    return DocumentEvidence(
        document_id="table",
        document_hash="hash",
        reader_name="test",
        pages=[PageContent(page_number=0, tables=[table])],
    )


# Tables that only look like trial tables keep the reading they had before trial layouts were
# understood: a text code is not a not-reported mark, a currency row is not a header, a reader's
# section is not merged with an inferred one, a year under a spanning header names its column,
# and a unit is not an estimator label.
@pytest.mark.parametrize(
    ("rows", "sections", "field", "expected"),
    [
        pytest.param(
            [["Region", "NA"], ["Code", "NE"]],
            None,
            {"type": "text", "locate": {"strategy": "table_label", "labels": ["Region"]}},
            ("extracted", "NA"),
            id="text-field-keeps-NA",
        ),
        pytest.param(
            [
                ["Item", "Description", "Amount"],
                ["", "Amount due", "$10.00"],
                ["Total", "20", "30"],
            ],
            None,
            table(["Total"], column_labels=["Amount"]),
            ("extracted", 30),
            id="currency-row-is-not-a-header",
        ),
        pytest.param(
            [
                ["Outcome", "Value"],
                ["Primary", "N = 51"],
                ["Deaths", "1"],
                ["Secondary", ""],
                ["Deaths", "2"],
            ],
            [None, None, "Primary", None, "Secondary"],
            table(["Deaths"], section_labels=["Primary"]),
            ("extracted", 1),
            id="reader-section-ends-inferred-group",
        ),
        pytest.param(
            [["Outcome", "Value"], ["Primary", "N = 51"], ["Deaths", "1"], ["Deaths", "2"]],
            [None, None, "Primary", "Secondary"],
            table(["Deaths"], section_labels=["Primary"]),
            ("extracted", 1),
            id="reader-section-is-never-merged-with-inferred-group",
        ),
        pytest.param(
            [
                ["Outcome", "Value"],
                ["Primary", "N = 51"],
                ["Deaths", "1"],
                ["Adverse events", ""],
                ["Deaths", "2"],
            ],
            None,
            table(["Deaths"], section_labels=["Primary"]),
            ("extracted", 1),
            id="label-only-row-ends-inferred-group",
        ),
        pytest.param(
            [
                ["Metric", "Revenue", "", "Costs", ""],
                ["", "2023", "2024", "2023", "2024"],
                ["Total", "10", "20", "30", "40"],
            ],
            None,
            table(["Total"], column_labels=["Revenue 2024"]),
            ("extracted", 20),
            id="years-under-spans-name-columns",
        ),
        pytest.param(
            [["Weight", "kg:12 (34%)"]],
            None,
            table(["Weight"], source_shape="count_percentage", component="count"),
            ("invalid", None),
            id="unit-is-not-an-estimator-label",
        ),
    ],
)
def test_tables_that_only_look_like_trial_tables_keep_their_reading(
    rows, sections, field, expected
):
    spec = ExtractionSpecification.model_validate({"name": "Table", "fields": {"value": field}})
    result = extract_evidence(evidence_with(rows, sections), spec)
    (only,) = result.fields
    assert (only.status, result.data["value"]) == expected, only.message
