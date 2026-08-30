from __future__ import annotations

import json

import pymupdf

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource


def borderless_pdf() -> bytes:
    """Aligned text columns with no drawn rules — no table borders at all."""
    document = pymupdf.open()
    page = document.new_page(width=595, height=360)
    page.insert_text((48, 56), "Statement of account", fontsize=14)
    rows = [
        ("Statement number", "ACC-2026-4412"),
        ("Net total", "1,840.00"),
        ("Grand total", "2,189.60"),
    ]
    for index, (label, value) in enumerate(rows):
        y = 96 + index * 24
        page.insert_text((56, y), label, fontsize=10)
        page.insert_text((256, y), value, fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def test_borderless_table_falls_back_to_text_alignment(tmp_path) -> None:
    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Statement check",
            "sections": [
                {
                    "key": "s",
                    "label": "S",
                    "fields": [
                        {
                            "key": "number",
                            "label": "Statement number",
                            "reference": {"pointer": "/number"},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": ["Statement number"]},
                            "compare": {"operator": "exact", "normalizers": ["trim"]},
                        },
                        {
                            "key": "total",
                            "label": "Grand total",
                            "reference": {"pointer": "/total"},
                            "value": {"type": "decimal"},
                            "locate": {"strategy": "table_label", "labels": ["Grand total"]},
                            "compare": {
                                "operator": "numeric",
                                "normalizers": ["numeric_punctuation"],
                                "absolute_tolerance": 0.01,
                            },
                        },
                    ],
                }
            ],
        }
    ).encode()

    document_path = tmp_path / "statement.pdf"
    document_path.write_bytes(borderless_pdf())
    plan = SpecificationCompiler().compile(
        specification, ReferenceSource(b'{"number": "ACC-2026-4412", "total": 2189.6}', "r.json")
    )
    evidence = TextLayerDocumentReader().read(document_path, "statement")
    assert evidence.pages[0].tables, "text-alignment fallback should recover the table"
    comparisons = ComparisonEngine().compare(plan, SchemaExtractor().extract(plan, evidence))
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)
