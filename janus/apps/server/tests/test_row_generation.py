"""Row-pattern field generation: one field, one extraction per matching row.

A field with a `rows` generator replaces its single static definition with a
pattern over row labels: the extractor generates one extraction per matching
row LABEL in document order — when a label survives in several value columns,
only the label's best-scoring column generates — derives a row key from a
capture group, and the comparison binds each row to the key of the object the
reference pointer addresses."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus, CompilationError, ExtractionStatus
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource

X0, COL1, X1 = 48.0, 200.0, 380.0
TOP, ROW_HEIGHT = 80.0, 22.0
# Six rows share one shape; the last row does not match the pattern.
REGION_ROWS = [
    ("Region A usage", "120.5"),
    ("Region B usage", "98.0"),
    ("Region C usage", "45.2"),
    ("Region D usage", "77.25"),
    ("Region E usage", "12.0"),
    ("Region F usage", "301.75"),
]
GRID = [["Region", "Usage"], *REGION_ROWS, ["Combined total", "654.7"]]


def regions_reference(extra: dict[str, float] | None = None, drop: str | None = None) -> bytes:
    values = {row[0].split()[1]: float(row[1]) for row in REGION_ROWS}
    if drop is not None:
        values.pop(drop)
    if extra is not None:
        values.update(extra)
    return json.dumps({"usage_by_region": values}).encode()


def rows_spec(
    *,
    pattern: str = r"^Region ([A-Z]) usage$",
    row_key_from: str | None = None,
    max_rows: int = 50,
    pointer: str = "/usage_by_region",
    required: bool = True,
    strategy: str = "table_label",
    column_labels: list[str] | None = None,
    section_labels: list[str] | None = None,
    value_pattern: str | None = None,
    minimum_confidence: float = 0.6,
) -> bytes:
    if column_labels is None and strategy == "table_label":
        column_labels = ["Usage"]
    locate: dict[str, Any] = {
        "strategy": strategy,
        "minimum_confidence": minimum_confidence,
    }
    if column_labels is not None:
        locate["column_labels"] = column_labels
    if section_labels is not None:
        locate["section_labels"] = section_labels
    if value_pattern is not None:
        locate["value_pattern"] = value_pattern
    rows: dict[str, Any] = {"label_pattern": pattern, "max_rows": max_rows}
    if row_key_from is not None:
        rows["row_key_from"] = row_key_from
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Row generation check",
            "sections": [
                {
                    "key": "usage",
                    "label": "Usage",
                    "fields": [
                        {
                            "key": "region",
                            "label": "Region usage",
                            "reference": {"pointer": pointer, "required": required},
                            "value": {"type": "decimal"},
                            "locate": locate,
                            "compare": {
                                "operator": "numeric",
                                "normalizers": ["numeric_punctuation"],
                            },
                            "rows": rows,
                        }
                    ],
                }
            ],
        }
    ).encode()


def regions_table_pdf() -> bytes:
    """A ruled table: one header row, six region rows, one unrelated row."""
    document = pymupdf.open()
    page = document.new_page(width=440, height=340)
    page.insert_text((48, 52), "Regional usage", fontsize=14)
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


def regions_text_pdf() -> bytes:
    """The same region rows as label:value text lines, outside any table."""
    document = pymupdf.open()
    page = document.new_page(width=440, height=340)
    page.insert_text((48, 52), "Regional usage", fontsize=14)
    for index, (label, value) in enumerate(REGION_ROWS):
        page.insert_text((48, 100 + index * 20), f"{label}: {value}", fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def read(content: bytes) -> Any:
    with tempfile.TemporaryDirectory() as directory:
        document_path = Path(directory) / "document.pdf"
        document_path.write_bytes(content)
        return TextLayerDocumentReader().read(document_path, "document")


def run(spec: bytes, content: bytes, reference: bytes):
    plan = SpecificationCompiler().compile(spec, ReferenceSource(reference, "reference.json"))
    extraction = SchemaExtractor().extract(plan, read(content))
    return plan, extraction, ComparisonEngine().compare(plan, extraction)


# A multi-value-column table: every row label spans a Usage and a Cost column,
# so a per-column enumeration would generate two rows per label.
MULTI_COLUMN_GRID = [
    ["Region", "Usage", "Cost"],
    ["Region A usage", "120.5", "12.0"],
    ["Region B usage", "98.0", "9.5"],
    ["Region C usage", "45.2", "4.75"],
]
# Near-identical headers fuzz-match ~0.95, so the wrong column's penalty
# leaves it inside the 0.03 ambiguity gap — the resolution must be complete
# token sets, not the fuzzy margin.
NEAR_IDENTICAL_GRID = [
    ["Region", "Check I [4,000 iterations]", "Check II [5,000 iterations]"],
    ["Region A usage", "120.5", "12.0"],
    ["Region B usage", "98.0", "9.5"],
]


def ruled_table_pdf(grid: list[list[str]], *, width: float = 560.0) -> bytes:
    """A ruled table with one label column and one value column per entry of
    grid[0] beyond the first (fully bordered, or the detector drops the last
    column)."""
    xs = [X0] + [200.0 + 120.0 * index for index in range(len(grid[0]))]
    document = pymupdf.open()
    page = document.new_page(width=width, height=340)
    page.insert_text((48, 52), "Regional usage", fontsize=14)
    bottom = TOP + ROW_HEIGHT * len(grid)
    for index in range(len(grid) + 1):
        y = TOP + index * ROW_HEIGHT
        page.draw_line(pymupdf.Point(X0, y), pymupdf.Point(xs[-1], y), color=(0.6, 0.6, 0.6), width=0.7)
    for x in xs:
        page.draw_line(pymupdf.Point(x, TOP), pymupdf.Point(x, bottom), color=(0.6, 0.6, 0.6), width=0.7)
    for row_index, row in enumerate(grid):
        for column_index, text in enumerate(row):
            x = X0 + 8 if column_index == 0 else xs[column_index] + 8
            page.insert_text((x, TOP + row_index * ROW_HEIGHT + 15), text, fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def test_table_rows_generate_one_extraction_per_matching_label() -> None:
    """Six matching rows become six extractions with keys from the capture
    group; the non-matching total row generates nothing."""
    plan, extraction, comparisons = run(rows_spec(), regions_table_pdf(), regions_reference())
    assert [item.field_key for item in extraction.fields] == [f"region.{key}" for key in "ABCDEF"]
    assert all(item.status is ExtractionStatus.EXTRACTED for item in extraction.fields)
    assert [item.row_key for item in extraction.fields] == list("ABCDEF")
    assert all(item.row_generator_id == plan.fields[0].id for item in extraction.fields)
    assert all(item.location is not None for item in extraction.fields)
    assert [item.field_key for item in comparisons] == [f"region.{key}" for key in "ABCDEF"]
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_rows_field_generates_one_row_per_label_not_per_value_column() -> None:
    """On a multi-value-column table a rows field generates one row per
    MATCHED LABEL, from the label's best-scoring column only: column_labels
    genuinely selects the column, and no suffixed off-column rows appear —
    the cold-run trap where a reproducibility field silently received the
    neighbouring column's value."""
    reference = json.dumps({"usage_by_region": {"A": 120.5, "B": 98.0, "C": 45.2}}).encode()
    _, extraction, comparisons = run(
        rows_spec(), ruled_table_pdf(MULTI_COLUMN_GRID), reference
    )
    assert [item.field_key for item in extraction.fields] == [
        "region.A",
        "region.B",
        "region.C",
    ]
    assert all(item.status is ExtractionStatus.EXTRACTED for item in extraction.fields)
    assert [item.normalized_document_value for item in extraction.fields] == [
        "120.5",
        "98.0",
        "45.2",
    ]
    assert all(item.column_context == "Usage" for item in extraction.fields)
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_rows_field_column_tie_without_column_labels_is_ambiguous() -> None:
    """Without column_labels a label's value columns tie within the ambiguity
    gap and the row goes ambiguous — the same loudness a static field applies
    — instead of guessing a column or enumerating all of them."""
    spec = rows_spec(column_labels=[])
    reference = json.dumps({"usage_by_region": {"A": 120.5, "B": 98.0}}).encode()
    _, extraction, comparisons = run(spec, ruled_table_pdf(MULTI_COLUMN_GRID), reference)
    assert [item.field_key for item in extraction.fields] == ["region.A", "region.B", "region.C"]
    for row in extraction.fields:
        assert row.status is ExtractionStatus.AMBIGUOUS
        assert "Usage" in row.explanation
        assert "Cost" in row.explanation
        assert "column_labels" in row.explanation
        assert row.runner_up_score is not None
        # The rival column is named by header AND located: page and column
        # ordinal, in the explanation and in the artifact.
        assert "page 0, column 2" in row.explanation
        assert row.runner_up_text == "Cost"
        assert row.runner_up_location is not None
        assert row.runner_up_location.column == 2
        assert row.runner_up_location.page == 0
    by_key = {item.field_key: item for item in comparisons}
    assert by_key["region.A"].status is ComparisonStatus.AMBIGUOUS
    # The reference key without a decided row value stays un-matched, never a
    # silent guess.
    assert by_key["region.C"].status is ComparisonStatus.AMBIGUOUS


def test_rows_field_near_identical_headers_resolve_by_complete_column_match() -> None:
    """'Check I [4,000 iterations]' and 'Check II [5,000 iterations]' fuzz-
    match past 0.95, so the penalized wrong column sits INSIDE the ambiguity
    gap; the tie resolves because the requested column_labels are a complete
    token set of one header and not the other — no minimum_confidence
    tuning required."""
    spec = rows_spec(column_labels=["Check I [4,000 iterations]"])
    reference = json.dumps({"usage_by_region": {"A": 120.5, "B": 98.0}}).encode()
    _, extraction, comparisons = run(spec, ruled_table_pdf(NEAR_IDENTICAL_GRID), reference)
    assert [item.field_key for item in extraction.fields] == ["region.A", "region.B"]
    assert all(item.status is ExtractionStatus.EXTRACTED for item in extraction.fields)
    assert [item.normalized_document_value for item in extraction.fields] == ["120.5", "98.0"]
    assert all(item.column_context == "Check I [4,000 iterations]" for item in extraction.fields)
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_explanations_carry_the_score_breakdown() -> None:
    """Every extraction explanation shows the winning candidate's score
    breakdown — label term (pattern for rows), section and column factors,
    confidence, final score, and the configured floor — and the artifact
    carries the same numbers as optional fields, so tuning a threshold reads
    the run instead of bisecting blind."""
    reference = json.dumps({"usage_by_region": {"A": 120.5}}).encode()
    _, extraction, _ = run(
        rows_spec(max_rows=1), ruled_table_pdf(MULTI_COLUMN_GRID), reference
    )
    row = extraction.fields[0]
    assert row.status is ExtractionStatus.EXTRACTED
    assert "score: pattern x section 1.00 x column 1.00" in row.explanation
    assert "floor 0.60" in row.explanation
    assert row.label_score is None
    assert row.section_factor == 1.0
    assert row.column_factor == 1.0
    assert row.final_score == row.confidence
    assert row.minimum_confidence == 0.6
    assert row.runner_up_score is None

    # A static field's explanation shows the label term and the column factor
    # it paid; a below-floor miss shows the best score against the floor.
    static = json.dumps(
        {
            "schema_version": "1",
            "name": "Static score visibility",
            "sections": [
                {
                    "key": "usage",
                    "label": "Usage",
                    "fields": [
                        {
                            "key": "total",
                            "label": "Combined total",
                            "reference": {"pointer": "/total", "required": False},
                            "value": {"type": "decimal"},
                            "locate": {
                                "strategy": "table_label",
                                "labels": ["Combined total"],
                                "column_labels": ["Usage"],
                            },
                            "compare": {"operator": "numeric"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    _, static_extraction, _ = run(static, regions_table_pdf(), b"{}")
    field = static_extraction.fields[0]
    assert field.status is ExtractionStatus.EXTRACTED
    assert "score: label 1.00 x section 1.00 x column 1.00" in field.explanation
    assert field.label_score == 1.0
    assert field.final_score == field.confidence


def test_below_floor_miss_shows_the_best_score_against_the_floor() -> None:
    """A rows generator whose labels matched but whose every candidate fell
    below the floor reports the best survivor's score against the configured
    floor — the two numbers a threshold tune needs, not a bare 'nothing
    matched'."""
    spec = rows_spec(
        strategy="text_label",
        column_labels=None,
        section_labels=["Quarterly Summary"],
        minimum_confidence=0.99,
    )
    _, extraction, _ = run(spec, regions_text_pdf(), regions_reference())
    note = extraction.fields[0]
    assert note.status is ExtractionStatus.NOT_EXTRACTED
    assert note.row_key is None  # generator-level note: nothing cleared the floor
    assert "floor 0.99" in note.explanation
    assert "score: pattern x section 0.70" in note.explanation
    assert "no candidate survived selection" in note.explanation


def test_reference_object_binds_each_row_by_key() -> None:
    _, extraction, comparisons = run(rows_spec(), regions_table_pdf(), regions_reference())
    by_key = {item.field_key: item for item in comparisons}
    assert by_key["region.C"].reference_pointer == "/usage_by_region/C"
    assert by_key["region.C"].reference_value == 45.2
    assert by_key["region.C"].document_value == "45.2"
    assert extraction.fields[2].field_id != extraction.fields[3].field_id


def test_missing_reference_key_for_a_found_row_is_a_loud_mismatch() -> None:
    _, _, comparisons = run(rows_spec(), regions_table_pdf(), regions_reference(drop="C"))
    item = next(entry for entry in comparisons if entry.field_key == "region.C")
    assert item.status is ComparisonStatus.MISMATCH
    assert "ROW_NOT_IN_REFERENCE" in item.explanation
    assert "'C'" in item.explanation


def test_reference_key_with_no_document_row_is_a_loud_mismatch() -> None:
    _, _, comparisons = run(rows_spec(), regions_table_pdf(), regions_reference(extra={"Z": 5.0}))
    item = next(entry for entry in comparisons if entry.field_key == "region.Z")
    assert item.status is ComparisonStatus.MISMATCH
    assert item.extraction_status is ExtractionStatus.NOT_EXTRACTED
    assert "ROW_NOT_IN_DOCUMENT" in item.explanation
    assert item.reference_value == 5.0


def test_max_rows_cap_fires_loudly() -> None:
    _, extraction, comparisons = run(
        rows_spec(max_rows=4), regions_table_pdf(), regions_reference()
    )
    # Four rows generated; the generator-level note follows them; the two
    # dropped rows surface as ROW_NOT_IN_DOCUMENT mismatches.
    assert [item.field_key for item in extraction.fields] == [
        "region.A",
        "region.B",
        "region.C",
        "region.D",
        "region",
    ]
    assert extraction.fields[-1].row_key is None
    note = next(
        entry for entry in comparisons if entry.field_key == "region" and "E" not in entry.field_key
    )
    assert note.status is ComparisonStatus.NOT_COMPARED
    assert "ROW_LIMIT_EXCEEDED" in note.explanation
    assert "max_rows" in note.explanation
    dropped = {
        entry.field_key: entry for entry in comparisons if entry.field_key.endswith(("E", "F"))
    }
    assert set(dropped) == {"region.E", "region.F"}
    assert all(entry.status is ComparisonStatus.MISMATCH for entry in dropped.values())


def test_no_matching_label_is_reported_not_swallowed() -> None:
    spec = rows_spec(pattern=r"^Zone (\d) usage$")
    plan = SpecificationCompiler().compile(
        spec, ReferenceSource(b'{"usage_by_region": {}}', "reference.json")
    )
    extraction = SchemaExtractor().extract(plan, read(regions_table_pdf()))
    assert len(extraction.fields) == 1
    item = extraction.fields[0]
    assert item.status is ExtractionStatus.NOT_EXTRACTED
    assert item.row_key is None
    assert item.row_generator_id == plan.fields[0].id
    comparison = ComparisonEngine().compare(plan, extraction)[0]
    assert comparison.status is ComparisonStatus.NOT_COMPARED
    assert "rows.label_pattern" in comparison.explanation


def test_text_label_rows_generate_from_repeated_label_value_lines() -> None:
    spec = rows_spec(strategy="text_label", column_labels=None, minimum_confidence=0.3)
    _, extraction, comparisons = run(spec, regions_text_pdf(), regions_reference())
    assert [item.field_key for item in extraction.fields] == [f"region.{key}" for key in "ABCDEF"]
    assert all(item.status is ExtractionStatus.EXTRACTED for item in extraction.fields)
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_named_group_and_index_group_both_derive_keys() -> None:
    named = rows_spec(pattern=r"^Region (?P<region>[A-Z]) usage$", row_key_from="region")
    _, extraction, _ = run(named, regions_table_pdf(), regions_reference())
    assert [item.row_key for item in extraction.fields] == list("ABCDEF")
    indexed = rows_spec(pattern=r"^Region ([A-Z]) usage$", row_key_from="1")
    _, extraction, _ = run(indexed, regions_table_pdf(), regions_reference())
    assert [item.row_key for item in extraction.fields] == list("ABCDEF")


def test_row_ids_are_stable_and_include_the_row_key() -> None:
    plan = SpecificationCompiler().compile(
        rows_spec(), ReferenceSource(regions_reference(), "r.json")
    )
    evidence = read(regions_table_pdf())
    first = SchemaExtractor().extract(plan, evidence)
    second = SchemaExtractor().extract(plan, evidence)
    assert [item.field_id for item in first.fields] == [item.field_id for item in second.fields]
    # The generator's own id differs from every row id, and two rows of the
    # same field never share an id.
    assert plan.fields[0].id not in {item.field_id for item in first.fields}
    assert len({item.field_id for item in first.fields}) == len(first.fields)
    # Changing the captured key changes the id: the row key is a hash input.
    swapped_plan = SpecificationCompiler().compile(
        rows_spec(pattern=r"^Region ([A-ZX]) usage$"),
        ReferenceSource(regions_reference(), "r.json"),
    )
    swapped = SchemaExtractor().extract(swapped_plan, read(regions_table_pdf()))
    assert swapped.fields[0].field_id != first.fields[0].field_id


def test_row_keys_are_sanitized_into_the_key_charset() -> None:
    """A captured key with characters outside the key charset ("/", spaces)
    sanitizes to underscores, and the reference binds the sanitized key."""
    document = pymupdf.open()
    page = document.new_page(width=440, height=240)
    page.insert_text((48, 60), "Regional usage", fontsize=12)
    for index, (label, value) in enumerate(
        [("Region North/Upper zone usage", "120.5"), ("Region South zone usage", "98.0")]
    ):
        page.insert_text((48, 100 + index * 20), f"{label}: {value}", fontsize=10)
    content = document.tobytes()
    document.close()
    spec = rows_spec(
        pattern=r"^Region (?P<zone>.+) usage$",
        strategy="text_label",
        column_labels=None,
        minimum_confidence=0.3,
    )
    reference = json.dumps(
        {"usage_by_region": {"North_Upper_zone": 120.5, "South_zone": 98.0}}
    ).encode()
    _, extraction, comparisons = run(spec, content, reference)
    assert [item.field_key for item in extraction.fields] == [
        "region.North_Upper_zone",
        "region.South_zone",
    ]
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_colliding_row_keys_get_a_numeric_suffix_and_a_warning() -> None:
    document = pymupdf.open()
    page = document.new_page(width=440, height=240)
    page.insert_text((48, 60), "Repeated region rows", fontsize=12)
    for index, (label, value) in enumerate([*REGION_ROWS[:2], ("Region A usage", "55.0")]):
        page.insert_text((48, 100 + index * 20), f"{label}: {value}", fontsize=10)
    content = document.tobytes()
    document.close()
    spec = rows_spec(strategy="text_label", column_labels=None, minimum_confidence=0.3)
    reference = json.dumps({"usage_by_region": {"A": 120.5, "A_2": 55.0}}).encode()
    plan = SpecificationCompiler().compile(spec, ReferenceSource(reference, "r.json"))
    extraction = SchemaExtractor().extract(plan, read(content))
    assert [item.field_key for item in extraction.fields] == ["region.A", "region.B", "region.A_2"]
    assert "already used" in extraction.fields[2].explanation
    comparisons = ComparisonEngine().compare(plan, extraction)
    by_key = {item.field_key: item for item in comparisons}
    assert by_key["region.A"].status is ComparisonStatus.MATCH
    assert by_key["region.A_2"].status is ComparisonStatus.MATCH
    # The repeated label's second occurrence is NOT silently merged: B has no
    # reference key here, which stays loud.
    assert by_key["region.B"].status is ComparisonStatus.MISMATCH


def test_locate_value_pattern_filters_rows_not_breaks_them() -> None:
    """For a rows field, value_pattern joins the row selector: a candidate
    whose value breaks the shape is not a row (no key consumed, no noise
    entry), and a reference key that loses its row that way stays loud as a
    ROW_NOT_IN_DOCUMENT mismatch."""
    document = pymupdf.open()
    page = document.new_page(width=440, height=240)
    page.insert_text((48, 60), "Regional usage", fontsize=12)
    rows = [("Region A usage", "120.5"), ("Region B usage", "N/A"), ("Region C usage", "45.2")]
    for index, (label, value) in enumerate(rows):
        page.insert_text((48, 100 + index * 20), f"{label}: {value}", fontsize=10)
    content = document.tobytes()
    document.close()
    spec = rows_spec(
        strategy="text_label",
        column_labels=None,
        minimum_confidence=0.3,
        value_pattern=r"^\d+\.\d+$",
    )
    _, extraction, comparisons = run(spec, content, regions_reference())
    assert [item.field_key for item in extraction.fields] == ["region.A", "region.C"]
    by_key = {item.field_key: item for item in comparisons}
    assert by_key["region.A"].status is ComparisonStatus.MATCH
    assert by_key["region.C"].status is ComparisonStatus.MATCH
    assert by_key["region.B"].status is ComparisonStatus.MISMATCH
    assert "ROW_NOT_IN_DOCUMENT" in by_key["region.B"].explanation


def test_value_pattern_keeps_repeated_labels_on_other_pages_from_flooding() -> None:
    """The flagship trap: the same label family printed under many tables. A
    value shape that only the target table's values fit keeps generation to
    the real rows instead of colliding every unrelated occurrence."""
    document = pymupdf.open()
    page = document.new_page(width=440, height=260)
    page.insert_text((48, 60), "Quarterly usage", fontsize=12)
    lines = [
        ("Region A usage: 120.5", 0),
        ("Region A usage: 31", 1),
        ("Region A usage: N/A", 2),
        ("Region B usage: 98.0", 3),
        ("Region B usage: 12", 4),
    ]
    for text, offset in lines:
        page.insert_text((48, 100 + offset * 20), text, fontsize=10)
    content = document.tobytes()
    document.close()
    spec = rows_spec(
        strategy="text_label",
        column_labels=None,
        minimum_confidence=0.3,
        value_pattern=r"^\d+\.\d+$",
    )
    reference = json.dumps({"usage_by_region": {"A": 120.5, "B": 98.0}}).encode()
    _, extraction, comparisons = run(spec, content, reference)
    assert [item.field_key for item in extraction.fields] == ["region.A", "region.B"]
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


# --- compilation gates ---------------------------------------------------------


def test_invalid_label_pattern_is_a_compilation_diagnostic() -> None:
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            rows_spec(pattern="(unbalanced"), ReferenceSource(b'{"usage_by_region": {}}', "r.json")
        )
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code == "INVALID_SPECIFICATION"
    assert "rows.label_pattern" in diagnostic.message


def test_pattern_without_a_capture_group_is_rejected() -> None:
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            rows_spec(pattern="^Region usage$"),
            ReferenceSource(b'{"usage_by_region": {}}', "r.json"),
        )
    assert "capture group" in caught.value.diagnostics[0].message


def test_row_key_from_naming_an_unknown_group_is_rejected() -> None:
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            rows_spec(row_key_from="zone"), ReferenceSource(b'{"usage_by_region": {}}', "r.json")
        )
    assert "rows.row_key_from" in caught.value.diagnostics[0].message


def test_pointer_at_a_non_object_fails_compilation_for_rows_fields() -> None:
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            rows_spec(pointer="/usage_by_region/0"),
            ReferenceSource(b'{"usage_by_region": [1, 2]}', "r.json"),
        )
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code == "REFERENCE_POINTER_NOT_OBJECT"
    assert diagnostic.reference_pointer == "/usage_by_region/0"
    assert diagnostic.specification_path == "/sections/0/fields/0/reference/pointer"


def test_optional_absent_pointer_lets_rows_extract_without_comparing() -> None:
    _, extraction, comparisons = run(
        rows_spec(pointer="/missing", required=False),
        regions_table_pdf(),
        b"{}",
    )
    assert len(extraction.fields) == 6
    assert all(item.status is ExtractionStatus.EXTRACTED for item in extraction.fields)
    assert all(item.status is ComparisonStatus.NOT_COMPARED for item in comparisons)


def test_rows_field_rejects_locate_labels() -> None:
    spec = json.loads(rows_spec())
    spec["sections"][0]["fields"][0]["locate"]["labels"] = ["Region usage"]
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            json.dumps(spec).encode(), ReferenceSource(b'{"usage_by_region": {}}', "r.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_rows_field_rejects_the_text_span_strategy() -> None:
    spec = json.loads(rows_spec(strategy="text_span"))
    spec["sections"][0]["fields"][0]["locate"]["labels"] = ["Regional usage"]
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            json.dumps(spec).encode(), ReferenceSource(b'{"usage_by_region": {}}', "r.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_field_without_rows_still_requires_labels() -> None:
    spec = json.loads(rows_spec())
    del spec["sections"][0]["fields"][0]["rows"]
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            json.dumps(spec).encode(), ReferenceSource(b'{"usage_by_region": {}}', "r.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_max_rows_bounds_are_enforced_by_the_schema() -> None:
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            rows_spec(max_rows=0), ReferenceSource(b'{"usage_by_region": {}}', "r.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            rows_spec(max_rows=201), ReferenceSource(b'{"usage_by_region": {}}', "r.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_extraction_artifact_round_trips_row_keys(tmp_path) -> None:
    from janus.store.artifact_store import ReviewArtifactStore

    _, extraction, _ = run(rows_spec(), regions_table_pdf(), regions_reference())
    store = ReviewArtifactStore(results_dir=tmp_path)
    store.save_extraction("review-1", extraction)
    loaded = store.load_extraction("review-1")
    assert [item.row_key for item in loaded.fields] == list("ABCDEF")
    assert all(item.row_generator_id is not None for item in loaded.fields)
