from __future__ import annotations

import json

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus, CompilationError
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource


def _spec(fields: list[dict]) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Grammar amendment check",
            "sections": [{"key": "s", "label": "S", "fields": fields}],
        }
    ).encode()


def _evidence(rows: list[tuple[str, str]], tmp_path, name="doc"):
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=420, height=60 + 24 * len(rows))
    x0, col1, x1 = 40, 200, 380
    for index, (label, value) in enumerate(rows):
        top = 40 + index * 24
        page.draw_rect(pymupdf.Rect(x0, top, x1, top + 24), color=(0.6, 0.6, 0.6), width=0.7)
        page.draw_line(
            pymupdf.Point(col1, top),
            pymupdf.Point(col1, top + 24),
            color=(0.6, 0.6, 0.6),
            width=0.7,
        )
        page.insert_text((x0 + 8, top + 16), label, fontsize=10)
        page.insert_text((col1 + 8, top + 16), value, fontsize=10)
    path = tmp_path / f"{name}.pdf"
    path.write_bytes(document.tobytes())
    document.close()
    return TextLayerDocumentReader().read(path, name)


def _run(spec: bytes, reference: ReferenceSource, evidence) -> list:
    plan = SpecificationCompiler().compile(spec, reference)
    return ComparisonEngine().compare(plan, SchemaExtractor().extract(plan, evidence))


def test_currency_symbol_normalizer(tmp_path) -> None:
    comparisons = _run(
        _spec(
            [
                {
                    "key": "total",
                    "label": "Total",
                    "reference": {"pointer": "/total"},
                    "value": {"type": "decimal"},
                    "locate": {"strategy": "table_label", "labels": ["Total"]},
                    "compare": {
                        "operator": "numeric",
                        "normalizers": ["currency_symbol", "numeric_punctuation"],
                        "absolute_tolerance": 0,
                    },
                }
            ]
        ),
        ReferenceSource(b'{"total": 2189.6}', "r.json"),
        _evidence([("Total", "$2,189.60")], tmp_path),
    )
    assert comparisons[0].status is ComparisonStatus.MATCH


def test_lookup_pointer_binds_by_field_identity(tmp_path) -> None:
    fields = [
        {
            "key": "number",
            "label": "Statement number",
            "reference": {"pointer": "/lookup/statement_number/value"},
            "value": {"type": "text"},
            "locate": {"strategy": "table_label", "labels": ["Statement number"]},
            "compare": {"operator": "exact", "normalizers": ["trim"]},
        }
    ]
    reference = ReferenceSource(
        b"field,value\npo_number,PO-1\nstatement_number,ACC-7\ntotal,120.5\n",
        "r.csv",
    )
    comparisons = _run(
        _spec(fields),
        reference,
        _evidence(
            [("PO number", "PO-1"), ("Statement number", "ACC-7"), ("Total", "120.5")],
            tmp_path,
        ),
    )
    assert comparisons[0].status is ComparisonStatus.MATCH
    assert comparisons[0].document_value == "ACC-7"


def test_lookup_pointer_missing_key_fails_with_pointer_diagnostic() -> None:
    try:
        SpecificationCompiler().compile(
            _spec(
                [
                    {
                        "key": "f",
                        "label": "F",
                        "reference": {"pointer": "/lookup/absent_field/value"},
                        "value": {"type": "text"},
                        "locate": {"strategy": "table_label", "labels": ["F"]},
                        "compare": {"operator": "exact"},
                    }
                ]
            ),
            ReferenceSource(b"field,value\nother,1\n", "r.csv"),
        )
    except CompilationError as exc:
        assert any(d.code == "REFERENCE_POINTER_NOT_FOUND" for d in exc.diagnostics)
    else:
        raise AssertionError("expected a compilation error")


def test_date_operator_accepts_dotted_and_iso_mix(tmp_path) -> None:
    for document_date, reference_date in [
        ("12.08.2026", "2026-08-12"),
        ("2026-08-12", "12.08.2026"),
    ]:
        comparisons = _run(
            _spec(
                [
                    {
                        "key": "issued",
                        "label": "Issued",
                        "reference": {"pointer": "/issued"},
                        "value": {"type": "date"},
                        "locate": {"strategy": "table_label", "labels": ["Issued"]},
                        "compare": {"operator": "date"},
                    }
                ]
            ),
            ReferenceSource(json.dumps({"issued": reference_date}).encode(), "r.json"),
            _evidence([("Issued", document_date)], tmp_path, name=f"date-{document_date}"),
        )
        assert comparisons[0].status is ComparisonStatus.MATCH, document_date


def test_estimate_interval_components_are_schema_checked() -> None:
    def spec_with(component: str) -> bytes:
        return _spec(
            [
                {
                    "key": "effect",
                    "label": "Effect",
                    "reference": {"pointer": "/effect"},
                    "value": {
                        "type": "decimal",
                        "source_shape": "estimate_interval",
                        "component": component,
                    },
                    "locate": {"strategy": "table_label", "labels": ["Effect"]},
                    "compare": {"operator": "numeric"},
                }
            ]
        )

    reference = ReferenceSource(b'{"effect": 0.26}', "r.json")
    SpecificationCompiler().compile(spec_with("standard_error"), reference)
    try:
        SpecificationCompiler().compile(spec_with("bogus"), reference)
    except CompilationError as exc:
        assert any(d.code == "INVALID_SPECIFICATION_SCHEMA" for d in exc.diagnostics)
    else:
        raise AssertionError("bogus component must fail the schema")
