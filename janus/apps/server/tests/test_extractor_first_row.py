from __future__ import annotations

import json

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.content import PageContent, TableCell, TableStructure
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus
from janus.comparison.reference import ReferenceSource


def _spec(labels: list[str]) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Headerless check",
            "sections": [
                {
                    "key": "s",
                    "label": "S",
                    "fields": [
                        {
                            "key": "f",
                            "label": "F",
                            "reference": {"pointer": "/f"},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": labels},
                            "compare": {"operator": "exact", "normalizers": ["trim"]},
                        }
                    ],
                }
            ],
        }
    ).encode()


def _evidence(*rows: tuple[str, str]):
    cells = [
        TableCell(text=label, row=index, column=0) for index, (label, _) in enumerate(rows)
    ] + [TableCell(text=value, row=index, column=1) for index, (_, value) in enumerate(rows)]
    table = TableStructure(rows=len(rows), columns=2, cells=cells, header_row=True)
    return PageContent(page_number=0, tables=[table])


def test_first_row_is_a_candidate_even_when_flagged_as_header() -> None:
    # Invoices and statements have no header row; the reader still marks
    # row 0 as header_row, and the extractor must not discard its values.
    plan = SpecificationCompiler().compile(
        _spec(["Invoice number"]), ReferenceSource(b'{"f": "INV-7"}', "r.json")
    )
    from janus.comparison.models import DocumentEvidence

    evidence = DocumentEvidence(
        document_id="d",
        document_hash="h",
        reader_name="test",
        pages=[_evidence(("Invoice number", "INV-7"), ("Invoice date", "2026-08-12"))],
    )
    extraction = SchemaExtractor().extract(plan, evidence)
    comparisons = ComparisonEngine().compare(plan, extraction)
    assert comparisons[0].status is ComparisonStatus.MATCH
    assert comparisons[0].document_value == "INV-7"
