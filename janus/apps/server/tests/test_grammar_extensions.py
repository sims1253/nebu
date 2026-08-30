"""Grammar extensions: friendly keys in extraction artifacts, locate.value_pattern,
and statistic annotations (P/BF) as extractable compound components."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.content import PageContent, TableCell, TableStructure
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import (
    COMPOUND_COMPONENTS,
    ComparisonStatus,
    CompilationError,
    CompoundShape,
    DocumentEvidence,
    ExtractionResult,
    ExtractionStatus,
    FieldExtraction,
)
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource
from janus.store.artifact_store import ReviewArtifactStore

JANUS_ROOT = Path(__file__).parents[3]
CANONICAL_SCHEMA = JANUS_ROOT / "packages/contracts/schema/comparison-specification.v1.json"


def spec_bytes(fields: list[dict], section_key: str = "s") -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Grammar extension check",
            "sections": [{"key": section_key, "label": "S", "fields": fields}],
        }
    ).encode()


def table_field(
    key: str,
    label: str,
    pointer: str,
    *,
    value_pattern: str | None = None,
    value_extra: dict | None = None,
) -> dict[str, Any]:
    locate: dict[str, Any] = {"strategy": "table_label", "labels": [label]}
    if value_pattern is not None:
        locate["value_pattern"] = value_pattern
    field: dict[str, Any] = {
        "key": key,
        "label": label,
        "reference": {"pointer": pointer},
        "value": {"type": "text", **(value_extra or {})},
        "locate": locate,
        "compare": {"operator": "exact", "normalizers": ["trim"]},
    }
    return field


def evidence_with_table(
    rows: list[tuple[str, str]],
    header: tuple[str, str] = ("Item", "Value"),
    section: str = "estimates",
) -> DocumentEvidence:
    cells = [TableCell(text=text, row=0, column=column) for column, text in enumerate(header)]
    for row_index, (label, value) in enumerate(rows, start=1):
        cells.append(TableCell(text=label, row=row_index, column=0, section=section))
        cells.append(TableCell(text=value, row=row_index, column=1, section=section))
    table = TableStructure(rows=1 + len(rows), columns=2, cells=cells, header_row=True)
    return DocumentEvidence(
        document_id="document",
        document_hash="hash",
        reader_name="test",
        pages=[PageContent(page_number=0, tables=[table])],
    )


def run(spec: bytes, evidence: DocumentEvidence, reference: bytes):
    plan = SpecificationCompiler().compile(spec, ReferenceSource(reference, "reference.json"))
    extraction = SchemaExtractor().extract(plan, evidence)
    return plan, extraction, ComparisonEngine().compare(plan, extraction)


# --- Task 1: friendly field keys in extraction artifacts ----------------------


def test_extraction_artifacts_carry_field_and_section_keys() -> None:
    """field_key and section_key are stamped at extraction time, so an
    extraction artifact reads without a join through comparison results."""
    spec = spec_bytes(
        [table_field("estimate", "Comparison", "/a/estimate")],
        section_key="estimates",
    )
    _, extraction, _ = run(
        spec, evidence_with_table([("Comparison", "0.26")]), b'{"a": {"estimate": 1}}'
    )
    item = extraction.fields[0]
    assert item.field_key == "estimate"
    assert item.section_key == "estimates"
    assert item.status is ExtractionStatus.EXTRACTED


def test_extraction_artifacts_carry_keys_on_failures_too() -> None:
    spec = spec_bytes([table_field("estimate", "Comparison", "/a/estimate")])
    _, extraction, _ = run(
        spec, evidence_with_table([("Unrelated", "0.26")]), b'{"a": {"estimate": 1}}'
    )
    item = extraction.fields[0]
    assert item.status is ExtractionStatus.NOT_EXTRACTED
    assert item.field_key == "estimate"
    assert item.section_key == "s"


def test_old_extraction_artifact_without_keys_still_validates() -> None:
    """The keys are additive and optional: an artifact JSON written before they
    existed model_validates unchanged (artifact schema version stays \"2\")."""
    legacy = {
        "field_id": "abc123",
        "status": "not_extracted",
        "raw_document_value": "0.26",
        "normalized_document_value": None,
        "location": None,
        "confidence": 0.9,
        "explanation": "legacy artifact without friendly keys",
        "column_context": None,
        "row_context": None,
    }
    item = FieldExtraction.model_validate(legacy)
    assert item.field_key is None
    assert item.section_key is None
    artifact = ExtractionResult(
        specification_id="spec", extraction_hash="ext", document_hash="doc", fields=[item]
    )
    assert artifact.artifact_schema_version == "2"


def test_artifact_store_round_trips_friendly_keys(tmp_path) -> None:
    store = ReviewArtifactStore(results_dir=tmp_path)
    extraction = ExtractionResult(
        specification_id="spec",
        extraction_hash="exthash",
        document_hash="dochash",
        fields=[
            FieldExtraction(
                field_id="f1",
                field_key="order_number",
                section_key="summary",
                status=ExtractionStatus.EXTRACTED,
                raw_document_value="PO-1042",
                normalized_document_value="PO-1042",
                confidence=1.0,
                explanation="Selected table cell using table_label with confidence 1.000.",
            )
        ],
    )
    store.save_extraction("review-1", extraction)
    for loaded in (
        store.load_extraction("review-1"),
        store.load_cached_extraction("dochash", "exthash"),
    ):
        assert loaded.fields[0].field_key == "order_number"
        assert loaded.fields[0].section_key == "summary"


# --- Task 2: locate.value_pattern --------------------------------------------


def portal_document() -> bytes:
    """Two pages. Page 0 prints the portal url with a label the alias does not
    reach; page 1 repeats a contact label with an email on the same baseline —
    the confidently-wrong candidate a lowered threshold admits."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=300)
    page.insert_text((48, 60), "Vendor portal", fontsize=14)
    page.insert_text((48, 100), "Web: https://portal.vendor.example/dashboard", fontsize=10)
    other = document.new_page(width=612, height=300)
    other.insert_text((48, 60), "Billing contact", fontsize=14)
    other.insert_text((48, 100), "Contact: ops@vendor.example", fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def wrapped_url_document() -> bytes:
    """A long url printed over two lines under one label; a text_label value
    keeps only the first line, so the candidate value is the truncated url."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=300)
    page.insert_text((48, 60), "Resource: https://portal.vendor.example/reports/", fontsize=10)
    page.insert_text((48, 74), "annual-report-2026.pdf", fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def read(document: bytes, document_id: str = "doc") -> DocumentEvidence:
    with tempfile.TemporaryDirectory() as directory:
        document_path = Path(directory) / "document.pdf"
        document_path.write_bytes(document)
        return TextLayerDocumentReader().read(document_path, document_id)


def text_field(
    key: str,
    label: str,
    pointer: str,
    *,
    value_pattern: str | None = None,
    minimum_confidence: float = 0.3,
) -> dict[str, Any]:
    locate: dict[str, Any] = {
        "strategy": "text_label",
        "labels": [label],
        "minimum_confidence": minimum_confidence,
    }
    if value_pattern is not None:
        locate["value_pattern"] = value_pattern
    return {
        "key": key,
        "label": label,
        "reference": {"pointer": pointer},
        "value": {"type": "text"},
        "locate": locate,
        "compare": {"operator": "exact", "normalizers": ["trim"]},
    }


def test_without_value_pattern_the_wrong_email_extracts() -> None:
    """The trap the pattern exists to close: without it, the best candidate's
    email is extracted silently and handed to the comparison."""
    spec = spec_bytes([text_field("portal", "Contact", "/url")])
    _, extraction, comparisons = run(spec, read(portal_document()), b'{"url": "https://x"}')
    assert extraction.fields[0].status is ExtractionStatus.EXTRACTED
    assert extraction.fields[0].raw_document_value == "ops@vendor.example"
    assert comparisons[0].status is ComparisonStatus.MISMATCH


def test_value_pattern_rejects_the_wrong_email_loudly() -> None:
    spec = spec_bytes([text_field("portal", "Contact", "/url", value_pattern="^https?://")])
    _, extraction, comparisons = run(spec, read(portal_document()), b'{"url": "https://x"}')
    item = extraction.fields[0]
    assert item.status is ExtractionStatus.NOT_EXTRACTED
    assert item.raw_document_value == "ops@vendor.example"
    assert "^https?://" in item.explanation
    assert comparisons[0].status is ComparisonStatus.NOT_COMPARED


def test_value_pattern_lets_the_matching_url_through() -> None:
    document = pymupdf.open()
    page = document.new_page(width=612, height=300)
    page.insert_text((48, 100), "Web: https://portal.vendor.example/dashboard", fontsize=10)
    content = document.tobytes()
    document.close()
    spec = spec_bytes([text_field("portal", "Web", "/url", value_pattern="^https?://")])
    _, extraction, _ = run(
        spec, read(content), b'{"url": "https://portal.vendor.example/dashboard"}'
    )
    assert extraction.fields[0].status is ExtractionStatus.EXTRACTED
    assert extraction.fields[0].raw_document_value == "https://portal.vendor.example/dashboard"


def test_value_pattern_catches_a_wrapped_value_truncation() -> None:
    """A pattern requiring the full form turns the silent first-line
    truncation into a loud not_extracted."""
    pattern = r"^https?://\S+\.pdf$"
    without = spec_bytes([text_field("resource", "Resource", "/url")])
    _, extraction, _ = run(without, read(wrapped_url_document()), b'{"url": "https://x"}')
    assert extraction.fields[0].status is ExtractionStatus.EXTRACTED
    assert extraction.fields[0].raw_document_value == "https://portal.vendor.example/reports/"

    with_pattern = spec_bytes([text_field("resource", "Resource", "/url", value_pattern=pattern)])
    _, extraction, comparisons = run(
        with_pattern, read(wrapped_url_document()), b'{"url": "https://x"}'
    )
    item = extraction.fields[0]
    assert item.status is ExtractionStatus.NOT_EXTRACTED
    assert item.raw_document_value == "https://portal.vendor.example/reports/"
    assert repr(pattern) in item.explanation
    assert comparisons[0].status is ComparisonStatus.NOT_COMPARED


def test_value_pattern_applies_to_table_label_candidates() -> None:
    evidence = evidence_with_table([("Order number", "PO-1042")])
    spec = spec_bytes(
        [
            table_field("order_ok", "Order number", "/a", value_pattern=r"^PO-\d+$"),
        ]
    )
    _, extraction, _ = run(spec, evidence, b'{"a": "PO-1042"}')
    assert extraction.fields[0].status is ExtractionStatus.EXTRACTED

    spec = spec_bytes(
        [
            table_field("order_bad", "Order number", "/a", value_pattern=r"^PO-\d+$"),
        ]
    )
    _, extraction, _ = run(spec, evidence_with_table([("Order number", "1O-1O42")]), b'{"a": "x"}')
    item = extraction.fields[0]
    assert item.status is ExtractionStatus.NOT_EXTRACTED
    assert "value_pattern" in item.explanation


def test_invalid_value_pattern_is_a_compilation_diagnostic() -> None:
    spec = spec_bytes([text_field("portal", "Contact", "/url", value_pattern="(unbalanced")])
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(spec, ReferenceSource(b'{"url": "x"}', "r.json"))
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION"
    assert "value_pattern" in caught.value.diagnostics[0].message


def test_value_pattern_length_is_capped_by_the_schema() -> None:
    spec = spec_bytes([text_field("portal", "Contact", "/url", value_pattern="^" + "a" * 300)])
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(spec, ReferenceSource(b'{"url": "x"}', "r.json"))
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


# --- Task 3: statistic annotations as components ------------------------------

ANNOTATED_CELL = "0.261 (0.025) [0.213, 0.310]; P<0.001"
WRAPPED_ANNOTATED_CELL = "0.26\n[0.22, 0.31];\nP<0.001;\nBF>100"


def stats_field(key: str, label: str, pointer: str, shape: str, component: str) -> dict[str, Any]:
    return table_field(
        key,
        label,
        pointer,
        value_extra={"source_shape": shape, "component": component},
    )


def test_p_value_component_returns_the_raw_annotation() -> None:
    """The component returns the matched annotation after the P token — the
    inequality and number exactly as printed, not parsed into a number."""
    spec = spec_bytes([stats_field("p_value", "Comparison", "/p", "estimate_interval", "p_value")])
    _, extraction, comparisons = run(
        spec, evidence_with_table([("Comparison", ANNOTATED_CELL)]), b'{"p": "<0.001"}'
    )
    assert extraction.fields[0].status is ExtractionStatus.EXTRACTED
    assert extraction.fields[0].normalized_document_value == "<0.001"
    assert comparisons[0].status is ComparisonStatus.MATCH


def test_bayes_factor_component_returns_the_raw_annotation() -> None:
    spec = spec_bytes(
        [stats_field("bayes", "Comparison", "/bf", "estimate_interval", "bayes_factor")]
    )
    _, extraction, comparisons = run(
        spec, evidence_with_table([("Comparison", WRAPPED_ANNOTATED_CELL)]), b'{"bf": ">100"}'
    )
    assert extraction.fields[0].normalized_document_value == ">100"
    assert comparisons[0].status is ComparisonStatus.MATCH


def test_annotation_components_on_the_mean_shape() -> None:
    cell = "5.2 (0.8); BF=12.3"
    spec = spec_bytes(
        [stats_field("bf", "Comparison", "/bf", "mean_standard_deviation", "bayes_factor")]
    )
    _, extraction, _ = run(spec, evidence_with_table([("Comparison", cell)]), b'{"bf": "=12.3"}')
    assert extraction.fields[0].normalized_document_value == "=12.3"


def test_p_value_accepts_bare_decimal_and_equals_forms() -> None:
    cell = "0.26 [0.22, 0.31]; P=.025"
    spec = spec_bytes([stats_field("p", "Comparison", "/p", "estimate_interval", "p_value")])
    _, extraction, _ = run(spec, evidence_with_table([("Comparison", cell)]), b'{"p": "=.025"}')
    assert extraction.fields[0].normalized_document_value == "=.025"


def test_missing_annotation_with_requested_component_is_loud() -> None:
    cell = "0.261 (0.025) [0.213, 0.310]"
    spec = spec_bytes([stats_field("p_value", "Comparison", "/p", "estimate_interval", "p_value")])
    _, extraction, comparisons = run(spec, evidence_with_table([("Comparison", cell)]), b'{"p": 1}')
    item = extraction.fields[0]
    assert item.status is ExtractionStatus.NOT_EXTRACTED
    assert "p_value" in item.explanation
    assert comparisons[0].status is ComparisonStatus.NOT_COMPARED


def test_annotation_component_still_requires_the_declared_shape() -> None:
    spec = spec_bytes([stats_field("p_value", "Comparison", "/p", "estimate_interval", "p_value")])
    _, extraction, _ = run(spec, evidence_with_table([("Comparison", "P<0.001")]), b'{"p": 1}')
    item = extraction.fields[0]
    assert item.status is ExtractionStatus.NOT_EXTRACTED
    assert "estimate_interval" in item.explanation


def test_existing_components_are_unchanged_on_annotated_cells() -> None:
    fields = [
        stats_field(key, "Comparison", f"/a/{key}", shape, component)
        for key, shape, component in (
            ("estimate", "estimate_interval", "estimate"),
            ("lower", "estimate_interval", "lower"),
            ("upper", "estimate_interval", "upper"),
            ("error", "mean_standard_deviation", "standard_deviation"),
        )
    ]
    spec = spec_bytes(fields)
    reference = json.dumps(
        {"a": {"estimate": 0.261, "lower": 0.213, "upper": 0.310, "error": 0.025}}
    ).encode()
    _, extraction, _ = run(spec, evidence_with_table([("Comparison", ANNOTATED_CELL)]), reference)
    values = {item.field_key: item.normalized_document_value for item in extraction.fields}
    assert values == {
        "estimate": "0.261",
        "lower": "0.213",
        "upper": "0.310",
        "error": "0.025",
    }


def test_compound_components_mirror_the_canonical_schema() -> None:
    """COMPOUND_COMPONENTS and the schema's per-shape component enums must
    accept exactly the same components."""
    schema = json.loads(CANONICAL_SCHEMA.read_text())
    rules = schema["$defs"]["field"]["properties"]["value"]["allOf"]
    mirrored = {
        rule["if"]["properties"]["source_shape"]["const"]: frozenset(
            rule["then"]["properties"]["component"]["enum"]
        )
        for rule in rules
    }
    assert mirrored == {
        shape.value: components for shape, components in COMPOUND_COMPONENTS.items()
    }
    assert "p_value" in COMPOUND_COMPONENTS[CompoundShape.ESTIMATE_INTERVAL]
    assert "bayes_factor" in COMPOUND_COMPONENTS[CompoundShape.MEAN_STANDARD_DEVIATION]
    assert "p_value" not in COMPOUND_COMPONENTS[CompoundShape.COUNT_PERCENTAGE]
    assert "p_value" not in COMPOUND_COMPONENTS[CompoundShape.RANGE]
