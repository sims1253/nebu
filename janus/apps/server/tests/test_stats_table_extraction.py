"""Statistics tables: compound statistic cells, page-split tables, and
near-identical row labels (as in a hypothesis-test table with rows H1..H8)."""

from __future__ import annotations

import json

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.content import PageContent, TableCell, TableStructure
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus, DocumentEvidence, ExtractionStatus
from janus.comparison.reader import _carry_split_header, _expand_grouped_rows
from janus.comparison.reference import ReferenceSource


def spec_bytes(fields: list[dict], section_key: str = "estimates") -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Estimates check",
            "sections": [{"key": section_key, "label": "Estimates", "fields": fields}],
        }
    ).encode()


def evidence_with_table(*rows: list[str], header: list[str] | None = None) -> DocumentEvidence:
    """One single-page table; row 0 is the header."""
    header = header if header is not None else ["Comparison", "Baseline", "Reproduction"]
    cells = [TableCell(text=text, row=0, column=column) for column, text in enumerate(header)]
    for row_index, row in enumerate(rows, start=1):
        for column, text in enumerate(row):
            cells.append(TableCell(text=text, row=row_index, column=column))
    table = TableStructure(rows=1 + len(rows), columns=len(header), cells=cells, header_row=True)
    page = PageContent(page_number=0, tables=[table])
    return DocumentEvidence(
        document_id="document", document_hash="hash", reader_name="test", pages=[page]
    )


def run(spec: bytes, evidence: DocumentEvidence, reference: bytes):
    plan = SpecificationCompiler().compile(spec, ReferenceSource(reference, "reference.json"))
    extraction = SchemaExtractor().extract(plan, evidence)
    return plan, extraction, ComparisonEngine().compare(plan, extraction)


CONDITION_A_LABEL = (
    "Condition A: scores will be\nhigher under the grouped\ncondition than the solo\ncondition."
)
CONDITION_B_LABEL = (
    "Condition B: scores will be\nhigher under the grouped\ncondition than the solo\ncondition."
)
BRACKETED_CELL = "0.26\n[0.22, 0.31];\nP<0.001;\nBF>100"
ESTIMATE_ERROR_CELL = "0.261\n(0.025)\n[0.213, 0.310];\nP<0.001"


def stats_evidence() -> DocumentEvidence:
    """A borderless statistics table: wrapped multi-line labels and values."""
    return evidence_with_table(
        [CONDITION_A_LABEL, BRACKETED_CELL, ESTIMATE_ERROR_CELL],
        [
            CONDITION_B_LABEL,
            "0.04\n[0.00, 0.09];\nP=0.066;\nBF=0.52",
            "0.045\n(0.024)\n[-0.003, 0.091];\nP=0.066",
        ],
    )


def test_bracketed_interval_components_from_one_cell() -> None:
    """'0.26 [0.22, 0.31]; P<0.001; BF>100' carries estimate and both bounds."""
    fields = [
        {
            "key": f"estimate_{component}",
            "label": f"Condition A {component}",
            "reference": {"pointer": f"/a/{component}"},
            "value": {
                "type": "decimal",
                "source_shape": "estimate_interval",
                "component": component,
            },
            "locate": {
                "strategy": "table_label",
                "labels": [
                    "Condition A: scores will be higher under the grouped condition than the solo condition."
                ],
                "column_labels": ["Baseline"],
            },
            "compare": {"operator": "numeric", "absolute_tolerance": 0},
        }
        for component in ("estimate", "lower", "upper")
    ]
    reference = json.dumps({"a": {"estimate": 0.26, "lower": 0.22, "upper": 0.31}}).encode()
    _, extraction, comparisons = run(spec_bytes(fields), stats_evidence(), reference)
    assert all(field.status is ExtractionStatus.EXTRACTED for field in extraction.fields)
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_estimate_with_error_and_interval_parses() -> None:
    """'0.261 (0.025) [0.213, 0.310]; P<0.001' keeps all four components."""
    fields = [
        {
            "key": key,
            "label": f"Condition A {component}",
            "reference": {"pointer": f"/a/{key}"},
            "value": {"type": "decimal", "source_shape": shape, "component": component},
            "locate": {
                "strategy": "table_label",
                "labels": [
                    "Condition A: scores will be higher under the grouped condition than the solo condition."
                ],
                "column_labels": ["Reproduction"],
            },
            "compare": {"operator": "numeric", "absolute_tolerance": 0},
        }
        for key, shape, component in (
            ("estimate", "estimate_interval", "estimate"),
            ("lower", "estimate_interval", "lower"),
            ("upper", "estimate_interval", "upper"),
            ("error", "mean_standard_deviation", "standard_deviation"),
        )
    ]
    reference = json.dumps(
        {"a": {"estimate": 0.261, "lower": 0.213, "upper": 0.310, "error": 0.025}}
    ).encode()
    _, _, comparisons = run(spec_bytes(fields), stats_evidence(), reference)
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons), [
        (item.field_key, item.status, item.document_value) for item in comparisons
    ]


def test_typeset_decimal_gap_still_parses() -> None:
    """A space after the decimal separator ('0. 261') must not block parsing."""
    evidence = evidence_with_table(
        ["Condition A", "0. 261\n(0.024)", ""],
    )
    fields = [
        {
            "key": "mean",
            "label": "Condition A mean",
            "reference": {"pointer": "/a/mean"},
            "value": {
                "type": "decimal",
                "source_shape": "mean_standard_deviation",
                "component": "mean",
            },
            "locate": {
                "strategy": "table_label",
                "labels": ["Condition A"],
                "column_labels": ["Baseline"],
            },
            "compare": {"operator": "numeric", "absolute_tolerance": 0},
        }
    ]
    _, extraction, comparisons = run(spec_bytes(fields), evidence, b'{"a": {"mean": 0.261}}')
    assert extraction.fields[0].normalized_document_value == "0.261"
    assert comparisons[0].status is ComparisonStatus.MATCH


def test_wrapped_statistic_row_is_not_split_into_grouped_rows() -> None:
    """A wrapped statistic ('0.26\\n[0.22, 0.31];\\nP<0.001') is one value, not
    a stack of sub-values, so its row must survive the grouped-row splitter."""
    structure = TableStructure(
        rows=2,
        columns=3,
        header_row=True,
        cells=[
            TableCell(text="Comparison", row=0, column=0),
            TableCell(text="Baseline", row=0, column=1),
            TableCell(text="Reproduction", row=0, column=2),
            TableCell(text=CONDITION_A_LABEL, row=1, column=0),
            TableCell(text=BRACKETED_CELL, row=1, column=1),
            TableCell(text=ESTIMATE_ERROR_CELL, row=1, column=2),
        ],
    )
    expanded = _expand_grouped_rows(structure)
    grid = {(cell.row, cell.column): cell for cell in expanded.cells}
    assert expanded.rows == 2
    assert grid[(1, 0)].text == CONDITION_A_LABEL
    assert grid[(1, 1)].text == BRACKETED_CELL


def _table(cells: list[TableCell], *, rows: int, columns: int) -> TableStructure:
    return TableStructure(rows=rows, columns=columns, cells=cells, header_row=True)


def test_split_table_carries_header_to_continuation_page() -> None:
    """A table that re-opens mid-row on the next page borrows the header of
    the page before, so its columns keep their context."""
    previous = _table(
        [
            TableCell(text="Comparison", row=0, column=0),
            TableCell(text="Baseline", row=0, column=1),
            TableCell(
                text="Condition A: scores will be\nhigher under the grouped", row=1, column=0
            ),
            TableCell(text="0.26\n[0.22, 0.31];", row=1, column=1),
        ],
        rows=2,
        columns=2,
    )
    continuation = _table(
        [
            TableCell(text="condition than", row=0, column=0),
            TableCell(text="", row=0, column=1),
            TableCell(text="Condition B: scores will be", row=1, column=0),
            TableCell(text="0.261\n(0.025)", row=1, column=1),
        ],
        rows=2,
        columns=2,
    )
    carried = _carry_split_header(previous, continuation)
    grid = {(cell.row, cell.column): cell for cell in carried.cells}
    assert carried.rows == 2
    assert grid[(0, 0)].text == "Comparison"
    assert grid[(0, 1)].text == "Baseline"
    assert grid[(1, 0)].text == "Condition B: scores will be"
    assert grid[(1, 1)].text == "0.261\n(0.025)"


def test_carry_leaves_tables_with_values_in_the_first_row_alone() -> None:
    previous = _table(
        [
            TableCell(text="Comparison", row=0, column=0),
            TableCell(text="Baseline", row=0, column=1),
            TableCell(text="Condition A: scores will be\nhigher under", row=1, column=0),
            TableCell(text="0.26\n[0.22, 0.31];", row=1, column=1),
        ],
        rows=2,
        columns=2,
    )
    current = _table(
        [
            TableCell(text="Condition A", row=0, column=0),
            TableCell(text="0.26", row=0, column=1),
        ],
        rows=1,
        columns=2,
    )
    assert _carry_split_header(previous, current) is current

    other_grid = _table(
        [
            TableCell(text="Comparison", row=0, column=0),
            TableCell(text="Baseline", row=0, column=1),
        ],
        rows=1,
        columns=2,
    )
    assert _carry_split_header(other_grid, current) is current


def test_near_identical_row_labels_resolve_by_complete_alias() -> None:
    """Rows differing by one token ('Condition A'/'Condition B') sit inside
    the ambiguity gap on similarity; a complete token-set match resolves it."""
    fields = [
        {
            "key": "estimate",
            "label": "Condition A estimate",
            "reference": {"pointer": "/a"},
            "value": {
                "type": "decimal",
                "source_shape": "estimate_interval",
                "component": "estimate",
            },
            "locate": {
                "strategy": "table_label",
                "labels": [
                    "Condition A: scores will be higher under the grouped condition than the solo condition."
                ],
                "column_labels": ["Baseline"],
            },
            "compare": {"operator": "numeric", "absolute_tolerance": 0},
        }
    ]
    _, extraction, comparisons = run(spec_bytes(fields), stats_evidence(), b'{"a": 0.26}')
    assert extraction.fields[0].status is ExtractionStatus.EXTRACTED
    assert comparisons[0].status is ComparisonStatus.MATCH
    assert comparisons[0].document_value == BRACKETED_CELL

    # An alias that both rows carry completely stays ambiguous.
    shared = [
        {
            **fields[0],
            "key": "shared",
            "reference": {"pointer": "/shared"},
            "locate": {
                "strategy": "table_label",
                "labels": [
                    "scores will be higher under the grouped condition than the solo condition."
                ],
                "column_labels": ["Baseline"],
            },
        }
    ]
    _, extraction, _ = run(spec_bytes(shared), stats_evidence(), b'{"shared": 0.26}')
    assert extraction.fields[0].status is ExtractionStatus.AMBIGUOUS


def rows_fields_spec(
    components: list[tuple[str, str, str]], *, column_labels: list[str] | None = None
) -> bytes:
    """One rows-generator field per (key, shape, component): the row pattern
    selects the hypothesis rows, one cell family per column."""
    return spec_bytes(
        [
            {
                "key": key,
                "label": f"Hypothesis {key}",
                "reference": {"pointer": f"/rows/{key}", "required": False},
                "value": {"type": "decimal", "source_shape": shape, "component": component},
                "locate": {
                    "strategy": "table_label",
                    "column_labels": column_labels or ["Baseline"],
                },
                "compare": {"operator": "numeric"},
                "rows": {"label_pattern": r"^H(?P<h>\d+):", "max_rows": 50},
            }
            for key, shape, component in components
        ]
    )


def mixed_interval_evidence() -> DocumentEvidence:
    """A hypothesis table whose cells mix the interval families: some carry a
    parenthesized error, some a bare bracketed interval only, some annotations
    and some none."""
    return evidence_with_table(
        [
            "H1: scores will be higher in the effort condition",
            "0.261 (0.025) [0.213, 0.310]; P<0.001; BF>100",
            "0.26 [0.22, 0.31]",
        ],
        [
            "H2: scores will be higher in the grouped condition",
            "0.34 (0.029) [0.29, 0.38]",
            "0.34 [0.29, 0.38]; P<0.001; BF>100",
        ],
    )


def test_rows_generator_never_crashes_on_component_misses() -> None:
    """A rows field with source_shape estimate_interval and component
    standard_error must not raise when a bare-interval cell ('0.34 [0.29,
    0.38]; P<0.001; BF>100' — no parenthesized error) is the winning cell of
    a matched label: the documented contract is a loud not_extracted row with
    an explanation, for annotation components (p_value/bayes_factor absent)
    and shape components the matched variant does not offer alike — while
    cells that do carry the requested component still extract."""
    spec = rows_fields_spec(
        [
            ("error", "estimate_interval", "standard_error"),
            ("p_value", "estimate_interval", "p_value"),
            ("bayes_factor", "estimate_interval", "bayes_factor"),
        ],
        column_labels=["Reproduction"],
    )
    plan = SpecificationCompiler().compile(spec, ReferenceSource(b"{}", "r.json"))
    extraction = SchemaExtractor().extract(plan, mixed_interval_evidence())
    by_field: dict[str, dict[str, object]] = {}
    for field in extraction.fields:
        prefix, row_key = field.field_key.split(".")
        by_field.setdefault(prefix, {})[row_key] = field
    # One row per matched label per field — no off-column suffixed duplicates.
    assert sorted(by_field) == ["bayes_factor", "error", "p_value"]
    for rows in by_field.values():
        assert sorted(rows) == ["1", "2"]
    # The Reproduction cells are bare intervals: standard_error is offered by
    # neither, loudly.
    for row in by_field["error"].values():
        assert row.status is ExtractionStatus.NOT_EXTRACTED
        assert "standard_error" in row.explanation
        assert row.row_generator_id is not None
        assert row.row_key in {"1", "2"}
    # H2's cell carries the annotations, H1's does not: present components
    # extract, absent ones are loud not_extracted rows.
    assert by_field["p_value"]["1"].status is ExtractionStatus.NOT_EXTRACTED
    assert "annotation" in by_field["p_value"]["1"].explanation
    assert by_field["p_value"]["2"].status is ExtractionStatus.EXTRACTED
    assert by_field["p_value"]["2"].normalized_document_value == "<0.001"
    assert by_field["bayes_factor"]["1"].status is ExtractionStatus.NOT_EXTRACTED
    assert "annotation" in by_field["bayes_factor"]["1"].explanation
    assert by_field["bayes_factor"]["2"].status is ExtractionStatus.EXTRACTED
    assert by_field["bayes_factor"]["2"].normalized_document_value == ">100"
    # And the parenthesized-error column still yields standard_error when it
    # is the column the field asks for.
    baseline = SchemaExtractor().extract(
        SpecificationCompiler().compile(
            rows_fields_spec([("error", "estimate_interval", "standard_error")]),
            ReferenceSource(b"{}", "r.json"),
        ),
        mixed_interval_evidence(),
    )
    assert [(row.field_key, row.status) for row in baseline.fields] == [
        ("error.1", ExtractionStatus.EXTRACTED),
        ("error.2", ExtractionStatus.EXTRACTED),
    ]
    assert [row.normalized_document_value for row in baseline.fields] == ["0.025", "0.029"]


def test_static_component_miss_on_bare_interval_is_not_extracted() -> None:
    """The same contract for a single static field: a component the matched
    variant does not offer is not_extracted, with an explanation naming it."""
    fields = [
        {
            "key": "error",
            "label": "Standard error",
            "reference": {"pointer": "/rows/error", "required": False},
            "value": {
                "type": "decimal",
                "source_shape": "estimate_interval",
                "component": "standard_error",
            },
            "locate": {
                "strategy": "table_label",
                "labels": ["H2: scores will be higher in the grouped condition"],
                "column_labels": ["Reproduction"],
            },
            "compare": {"operator": "numeric"},
        }
    ]
    _, extraction, _ = run(spec_bytes(fields), mixed_interval_evidence(), b"{}")
    field = extraction.fields[0]
    assert field.status is ExtractionStatus.NOT_EXTRACTED
    assert "standard_error" in field.explanation


def test_near_identical_column_labels_resolve_by_complete_alias() -> None:
    """'Check I [4,000 iterations]' and 'Check II [5,000 iterations]' differ
    by one token; the column that carries the requested token set wins."""
    evidence = evidence_with_table(
        ["Condition A", "0.337\n(0.024)", "0.338\n(0.024)"],
        header=["Comparison", "Check I\n[4,000 iterations]", "Check II\n[5,000 iterations]"],
    )
    fields = [
        {
            "key": "mean",
            "label": "Condition A mean, check I",
            "reference": {"pointer": "/a"},
            "value": {
                "type": "decimal",
                "source_shape": "mean_standard_deviation",
                "component": "mean",
            },
            "locate": {
                "strategy": "table_label",
                "labels": ["Condition A"],
                "column_labels": ["Check I [4,000 iterations]"],
            },
            "compare": {"operator": "numeric", "absolute_tolerance": 0},
        }
    ]
    _, extraction, comparisons = run(spec_bytes(fields), evidence, b'{"a": 0.337}')
    assert extraction.fields[0].status is ExtractionStatus.EXTRACTED
    assert comparisons[0].status is ComparisonStatus.MATCH
    assert comparisons[0].document_location is not None
    assert comparisons[0].document_location.column == 1


def test_genuine_row_label_tie_names_the_rival() -> None:
    """Two rows printing the same label with no disambiguator are a genuine
    tie: the field is ambiguous, and the explanation names the RIVAL — its
    row label and where it sits — instead of leaving the tied scores
    anonymous. The artifact carries the same naming."""
    evidence = evidence_with_table(
        ["Condition A", "0.26\n[0.22, 0.31]", "0.34\n[0.29, 0.38]"],
        ["Condition A", "0.261\n(0.025)", "0.341\n(0.029)"],
    )
    fields = [
        {
            "key": "estimate",
            "label": "Condition A estimate",
            "reference": {"pointer": "/a", "required": False},
            "value": {
                "type": "decimal",
                "source_shape": "estimate_interval",
                "component": "estimate",
            },
            "locate": {
                "strategy": "table_label",
                "labels": ["Condition A"],
                "column_labels": ["Baseline"],
            },
            "compare": {"operator": "numeric", "absolute_tolerance": 0},
        }
    ]
    _, extraction, _ = run(spec_bytes(fields), evidence, b'{"a": 0.26}')
    field = extraction.fields[0]
    assert field.status is ExtractionStatus.AMBIGUOUS
    # The rival is named: its text and its location (page, row, column).
    assert "runner-up 'Condition A' at page 0, row 2, column 'Baseline'" in field.explanation
    assert field.runner_up_score is not None
    assert field.runner_up_text == "Condition A"
    assert field.runner_up_location is not None
    assert field.runner_up_location.page == 0
    assert field.runner_up_location.row == 2
    assert field.runner_up_location.column == 1
