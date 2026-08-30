from __future__ import annotations

import io
import json
from importlib import resources
from pathlib import Path

import pytest
from openpyxl import Workbook

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.models import (
    ComparisonOperator,
    CompilationError,
    CompoundShape,
    Normalizer,
    Severity,
    ValueType,
)
from janus.comparison.reference import ReferenceSource, normalize_reference, resolve_json_pointer


def specification(pointer: str = "/order/id", *, required: bool = True) -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Order check",
            "sections": [
                {
                    "key": "summary",
                    "label": "Summary",
                    "fields": [
                        {
                            "key": "order_id",
                            "label": "Order identifier",
                            "reference": {"pointer": pointer, "required": required},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": ["Order number"]},
                            "compare": {"operator": "exact", "normalizers": ["trim"]},
                        }
                    ],
                }
            ],
        }
    ).encode()


def test_compiles_and_ids_are_stable_across_reference_revisions() -> None:
    compiler = SpecificationCompiler()
    first = compiler.compile(specification(), ReferenceSource(b'{"order":{"id":"A"}}', "a.json"))
    second = compiler.compile(specification(), ReferenceSource(b'{"order":{"id":"B"}}', "b.json"))
    assert first.fields[0].id == second.fields[0].id
    assert first.extraction_hash == second.extraction_hash
    assert first.comparison_hash != second.comparison_hash


def test_missing_pointer_is_actionable() -> None:
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(specification(), ReferenceSource(b"{}", "data.json"))
    diagnostic = caught.value.diagnostics[0]
    assert diagnostic.code == "REFERENCE_POINTER_NOT_FOUND"
    assert diagnostic.reference_pointer == "/order/id"
    assert diagnostic.specification_path == "/sections/0/fields/0/reference/pointer"


def test_optional_pointer_compiles_as_absent() -> None:
    plan = SpecificationCompiler().compile(
        specification(required=False), ReferenceSource(b"{}", "data.json")
    )
    assert plan.fields[0].expected_value_present is False


def test_unknown_properties_are_rejected() -> None:
    raw = json.loads(specification())
    raw["surprise"] = True
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            json.dumps(raw).encode(), ReferenceSource(b"{}", "data.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_rfc6901_escaping() -> None:
    assert resolve_json_pointer({"a/b": {"~key": 7}}, "/a~1b/~0key") == (True, 7)


def test_csv_normalization() -> None:
    tree, kind = normalize_reference(ReferenceSource(b"name,value\nA,10\n", "data.csv"))
    assert kind == "csv"
    assert tree["rows"] == [{"name": "A", "value": "10"}]
    assert tree["lookup"] == {"A": {"value": "10"}}


def test_xlsx_normalization() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Orders"
    sheet.append(["id", "total"])
    sheet.append(["PO-1", 12.5])
    stream = io.BytesIO()
    workbook.save(stream)
    tree, kind = normalize_reference(ReferenceSource(stream.getvalue(), "data.xlsx"))
    assert kind == "xlsx"
    assert tree["sheets"]["Orders"]["rows"] == [{"id": "PO-1", "total": 12.5}]


def test_bundled_schema_matches_the_canonical_schema() -> None:
    canonical = (
        Path(__file__).parents[3] / "packages/contracts/schema/comparison-specification.v1.json"
    )
    bundled = resources.files("janus.schemas").joinpath("comparison-specification.v1.json")
    assert bundled.read_bytes() == canonical.read_bytes()


def test_schema_invalid_specification_reports_diagnostics() -> None:
    specification = json.dumps(
        {
            "schema_version": "1",
            "name": "Broken",
            "sections": [
                {
                    "key": "s",
                    "label": "S",
                    "fields": [
                        {
                            "key": "f",
                            "label": "F",
                            "reference": {"pointer": "not-a-pointer"},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": ["F"]},
                            "compare": {"operator": "exact"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    with pytest.raises(CompilationError) as exc_info:
        SpecificationCompiler().compile(
            specification, ReferenceSource(b'{"f": 1}', "reference.json")
        )
    codes = [diagnostic.code for diagnostic in exc_info.value.diagnostics]
    assert "INVALID_SPECIFICATION_SCHEMA" in codes


def test_python_enums_match_the_canonical_schema() -> None:
    schema_path = (
        Path(__file__).parents[3] / "packages/contracts/schema/comparison-specification.v1.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    field = schema["$defs"]["field"]["properties"]
    comparison = schema["$defs"]["comparison"]["properties"]
    assert set(field["value"]["properties"]["type"]["enum"]) == set(ValueType)
    assert set(field["value"]["properties"]["source_shape"]["enum"]) == set(CompoundShape)
    assert set(comparison["operator"]["enum"]) == set(ComparisonOperator)
    assert set(comparison["normalizers"]["items"]["enum"]) == set(Normalizer)
    assert set(comparison["severity"]["enum"]) == set(Severity)
