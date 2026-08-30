from __future__ import annotations

import json

import pytest

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.content import PageContent, TableCell, TableStructure
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus, CompilationError, DocumentEvidence
from janus.comparison.reference import ReferenceSource


def spec_with_pointer(pointer: str) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Rows check",
            "sections": [
                {
                    "key": "rows",
                    "label": "Rows",
                    "fields": [
                        {
                            "key": "value",
                            "label": "Value",
                            "reference": {"pointer": pointer},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": ["Value"]},
                            "compare": {"operator": "exact"},
                        }
                    ],
                }
            ],
        }
    ).encode()


@pytest.mark.parametrize(
    ("cell_text", "reference_value", "expected_status"),
    [
        # Mixed separators: the later separator is the decimal point.
        ("1234.56", "1.234,56", ComparisonStatus.MATCH),
        ("1234.56", "1,234.56", ComparisonStatus.MATCH),
        # Repeated single separators are thousands separators.
        ("1234567", "1,234,567", ComparisonStatus.MATCH),
        ("1234567", "1.234.567", ComparisonStatus.MATCH),
        # A single separator is treated as a decimal point on both sides,
        # so equal text still matches and unequal values still mismatch.
        ("1.234", "1,234", ComparisonStatus.MATCH),
        ("1.5", "1,234", ComparisonStatus.MISMATCH),
    ],
)
def test_numeric_punctuation_separator_conventions(
    cell_text: str, reference_value: str, expected_status: ComparisonStatus
) -> None:
    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Totals check",
            "sections": [
                {
                    "key": "totals",
                    "label": "Totals",
                    "fields": [
                        {
                            "key": "total",
                            "label": "Grand total",
                            "reference": {"pointer": "/total"},
                            "value": {"type": "decimal"},
                            "locate": {"strategy": "table_label", "labels": ["Grand total"]},
                            "compare": {
                                "operator": "numeric",
                                "normalizers": ["numeric_punctuation"],
                                "absolute_tolerance": 0.0,
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    plan = SpecificationCompiler().compile(
        specification, ReferenceSource(json.dumps({"total": reference_value}).encode(), "r.json")
    )
    page = PageContent(
        page_number=0,
        tables=[
            TableStructure(
                rows=2,
                columns=2,
                header_row=True,
                cells=[
                    TableCell(text="Label", row=0, column=0),
                    TableCell(text="Value", row=0, column=1),
                    TableCell(text="Grand total", row=1, column=0),
                    TableCell(text=cell_text, row=1, column=1),
                ],
            )
        ],
    )
    evidence = DocumentEvidence(
        document_id="d", document_hash="h", reader_name="test", pages=[page]
    )
    extraction = SchemaExtractor().extract(plan, evidence)
    comparison = ComparisonEngine().compare(plan, extraction)[0]
    assert comparison.status is expected_status


@pytest.mark.parametrize("pointer", ["/rows/\u00b2", "/rows/01", "/rows/-1", "/rows/1.5"])
def test_invalid_array_pointer_tokens_fail_compilation_not_the_server(pointer: str) -> None:
    with pytest.raises(CompilationError):
        SpecificationCompiler().compile(
            spec_with_pointer(pointer), ReferenceSource(b'{"rows":["a","b"]}', "r.json")
        )


def test_corrupt_xlsx_fails_compilation_not_the_server() -> None:
    with pytest.raises(CompilationError):
        SpecificationCompiler().compile(
            spec_with_pointer("/rows/0/value"),
            ReferenceSource(b"this is not a zip archive", "reference.xlsx"),
        )


def test_csv_with_duplicate_headers_fails_compilation() -> None:
    with pytest.raises(CompilationError):
        SpecificationCompiler().compile(
            spec_with_pointer("/rows/0/id"), ReferenceSource(b"id,id\n1,2", "reference.csv")
        )


def test_agreement_rate_is_null_when_nothing_was_compared() -> None:
    summary = ComparisonEngine.summarize([])
    assert summary.agreement_rate is None
    assert summary.total_fields == 0


def test_reviewer_correction_persists_extraction_and_keeps_cache_pristine(tmp_path) -> None:
    from janus.comparison.models import Resolution, ReviewResult
    from janus.store.artifact_store import ReviewArtifactStore

    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Order check",
            "sections": [
                {
                    "key": "summary",
                    "label": "Summary",
                    "fields": [
                        {
                            "key": "number",
                            "label": "Order number",
                            "reference": {"pointer": "/order/id"},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": ["Order number"]},
                            "compare": {"operator": "exact"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    plan = SpecificationCompiler().compile(
        specification, ReferenceSource(b'{"order":{"id":"PO-1"}}', "r.json")
    )
    page = PageContent(
        page_number=0,
        tables=[
            TableStructure(
                rows=2,
                columns=2,
                header_row=True,
                cells=[
                    TableCell(text="Label", row=0, column=0),
                    TableCell(text="Value", row=0, column=1),
                    TableCell(text="Order number", row=1, column=0),
                    TableCell(text="PO-2", row=1, column=1),
                ],
            )
        ],
    )
    evidence = DocumentEvidence(
        document_id="d", document_hash="dochash", reader_name="test", pages=[page]
    )
    extraction = SchemaExtractor().extract(plan, evidence)
    comparisons = ComparisonEngine().compare(plan, extraction)
    result = ReviewResult(
        review_id="r1",
        specification_id=plan.specification_id,
        reference_hash=plan.reference_hash,
        extraction_hash=extraction.extraction_hash,
        comparisons=comparisons,
        summary=ComparisonEngine.summarize(comparisons),
        processing_time_seconds=0.1,
    )

    store = ReviewArtifactStore(results_dir=tmp_path / "artifacts")
    store.save_plan("r1", plan)
    store.save_evidence("r1", evidence)
    store.save_extraction("r1", extraction)
    store.save_result("r1", result)

    corrected = store.annotate_comparison(
        review_id="r1",
        comparison_id=comparisons[0].id,
        resolution=Resolution.INCORRECT_DOCUMENT_MATCH,
        notes=None,
        document_value="PO-1",
    )
    assert corrected is not None
    assert corrected.status is ComparisonStatus.MATCH
    assert corrected.confidence == 1.0

    persisted_extraction = store.load_extraction("r1")
    assert persisted_extraction.fields[0].raw_document_value == "PO-1"
    # The shared cache still holds the machine extraction, not the correction.
    cached = store.load_cached_extraction("dochash", extraction.extraction_hash)
    assert cached.fields[0].raw_document_value == "PO-2"
