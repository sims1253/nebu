from __future__ import annotations

import json

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.content import PageContent, TableCell, TableStructure
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus, DocumentEvidence, ExtractionStatus
from janus.comparison.reference import ReferenceSource


def make_spec(*, expected_pointer: str = "/total", tolerance: float = 0.01) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Invoice check",
            "sections": [
                {
                    "key": "totals",
                    "label": "Totals",
                    "fields": [
                        {
                            "key": "total",
                            "label": "Grand total",
                            "reference": {"pointer": expected_pointer},
                            "value": {"type": "decimal"},
                            "locate": {"strategy": "table_label", "labels": ["Grand total"]},
                            "compare": {
                                "operator": "numeric",
                                "normalizers": ["numeric_punctuation"],
                                "absolute_tolerance": tolerance,
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def evidence(value: str = "1,275.50") -> DocumentEvidence:
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
                    TableCell(text=value, row=1, column=1),
                ],
            )
        ],
    )
    return DocumentEvidence(
        document_id="document", document_hash="hash", reader_name="test", pages=[page]
    )


def test_extraction_is_independent_of_expected_value() -> None:
    compiler = SpecificationCompiler()
    first = compiler.compile(make_spec(), ReferenceSource(b'{"total":1275.5}', "one.json"))
    second = compiler.compile(make_spec(), ReferenceSource(b'{"total":999}', "two.json"))
    extractor = SchemaExtractor()
    one = extractor.extract(first, evidence())
    two = extractor.extract(second, evidence())
    assert one.fields == two.fields
    assert one.fields[0].status is ExtractionStatus.EXTRACTED


def test_numeric_tolerance_and_mismatch() -> None:
    plan = SpecificationCompiler().compile(
        make_spec(tolerance=0.01), ReferenceSource(b'{"total":1275.505}', "data.json")
    )
    extraction = SchemaExtractor().extract(plan, evidence())
    comparisons = ComparisonEngine().compare(plan, extraction)
    assert comparisons[0].status is ComparisonStatus.MATCH

    changed = SpecificationCompiler().compile(
        make_spec(tolerance=0.01), ReferenceSource(b'{"total":1300}', "data.json")
    )
    compared_again = ComparisonEngine().compare(changed, extraction)
    assert compared_again[0].status is ComparisonStatus.MISMATCH


def test_duplicate_labels_are_ambiguous() -> None:
    page = evidence().pages[0]
    page.tables.append(page.tables[0].model_copy(deep=True))
    plan = SpecificationCompiler().compile(
        make_spec(), ReferenceSource(b'{"total":1275.5}', "data.json")
    )
    extraction = SchemaExtractor().extract(
        plan,
        DocumentEvidence(document_id="d", document_hash="h", reader_name="test", pages=[page]),
    )
    assert extraction.fields[0].status is ExtractionStatus.AMBIGUOUS
    comparison = ComparisonEngine().compare(plan, extraction)[0]
    assert comparison.status is ComparisonStatus.AMBIGUOUS
