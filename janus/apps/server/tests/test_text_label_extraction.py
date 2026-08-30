from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.content import BoundingBox, PageContent, TextElement
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus, CompilationError, DocumentEvidence
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource


def device_data_sheet() -> bytes:
    """A header block of label/value lines: some print label and value in one
    line, some set the value in a column of its own on the same baseline, and
    a repeated label sits under two different numbered headings."""
    document = pymupdf.open()
    page = document.new_page(width=612, height=420)
    page.insert_text((48, 52), "Device data sheet", fontsize=14)
    lines = [
        "1. Specifications",
        "Product: GLM-4 evaluation unit",
        "Version: 8.0",
        "Operating temperature: -10.0 \u00b0C",
        "Accuracy: 0.26 (0.025)",
    ]
    for index, line in enumerate(lines):
        page.insert_text((48, 84 + index * 20), line, fontsize=10)
    # Label and value printed as separate lines on the same baseline.
    page.insert_text((48, 196), "Mass:", fontsize=10)
    page.insert_text((210, 196), "1.25 kg", fontsize=10)
    page.insert_text((48, 220), "Rated power:", fontsize=10)
    page.insert_text((210, 220), "45 W", fontsize=10)
    page.insert_text((48, 252), "2. History", fontsize=12)
    page.insert_text((48, 276), "Revision: A", fontsize=10)
    page.insert_text((48, 300), "Notes follow below.", fontsize=10)
    page.insert_text((48, 332), "1. Specifications", fontsize=12)
    page.insert_text((48, 356), "Revision: B", fontsize=10)
    page.insert_text((48, 380), "End of sheet", fontsize=10)
    content = document.tobytes()
    document.close()
    return content


def field(
    key: str,
    label: str,
    pointer: str,
    *,
    section_labels: list[str] | None = None,
    source_shape: str | None = None,
    component: str | None = None,
) -> dict[str, Any]:
    locate: dict[str, Any] = {"strategy": "text_label", "labels": [label]}
    if section_labels:
        locate["section_labels"] = section_labels
    value: dict[str, Any] = {"type": "text"}
    if source_shape:
        value["source_shape"] = source_shape
        value["component"] = component
    return {
        "key": key,
        "label": label,
        "reference": {"pointer": pointer},
        "value": value,
        "locate": locate,
        "compare": {"operator": "exact", "normalizers": ["trim"]},
    }


def specification() -> bytes:
    return json.dumps(
        {
            "schema_version": "1",
            "name": "Device data sheet check",
            "sections": [
                {
                    "key": "header",
                    "label": "Header",
                    "fields": [
                        field("product", "Product", "/header/product"),
                        field("version", "Version", "/header/version"),
                    ],
                },
                {
                    "key": "properties",
                    "label": "Properties",
                    "fields": [
                        field("mass", "Mass", "/properties/mass"),
                        field("rated_power", "Rated power", "/properties/rated_power"),
                        field("temperature", "Operating temperature", "/properties/temperature"),
                        field(
                            "accuracy",
                            "Accuracy",
                            "/properties/accuracy",
                            source_shape="mean_standard_deviation",
                            component="mean",
                        ),
                        field(
                            "accuracy_error",
                            "Accuracy",
                            "/properties/accuracy_error",
                            source_shape="mean_standard_deviation",
                            component="standard_deviation",
                        ),
                    ],
                },
                {
                    "key": "revisions",
                    "label": "Revisions",
                    "fields": [
                        field(
                            "revision_current",
                            "Revision",
                            "/revisions/current",
                            section_labels=["1. Specifications"],
                        ),
                        field(
                            "revision_previous",
                            "Revision",
                            "/revisions/previous",
                            section_labels=["2. History"],
                        ),
                    ],
                },
            ],
        }
    ).encode()


def reference() -> bytes:
    return json.dumps(
        {
            "header": {"product": "GLM-4 evaluation unit", "version": "8.0"},
            "properties": {
                "mass": "1.25 kg",
                "rated_power": "45 W",
                "temperature": "-10.0 \u00b0C",
                "accuracy": "0.26",
                "accuracy_error": "0.025",
            },
            "revisions": {"current": "B", "previous": "A"},
        }
    ).encode()


def read(document: bytes, document_id: str = "sheet") -> DocumentEvidence:
    with tempfile.TemporaryDirectory() as directory:
        document_path = Path(directory) / "sheet.pdf"
        document_path.write_bytes(document)
        return TextLayerDocumentReader().read(document_path, document_id)


def results(document: bytes) -> dict[str, Any]:
    plan = SpecificationCompiler().compile(
        specification(), ReferenceSource(reference(), "ref.json")
    )
    extraction = SchemaExtractor().extract(plan, read(document))
    by_id = {field.id: field.key for field in plan.fields}
    return {by_id[item.field_id]: item for item in extraction.fields}


def test_text_label_fields_extract_with_page_and_bbox() -> None:
    extracted = results(device_data_sheet())
    for key in ("product", "version", "mass", "rated_power", "temperature"):
        item = extracted[key]
        assert item.status.value == "extracted", f"{key}: {item.explanation}"
        assert item.location is not None
        assert item.location.page == 0
        assert item.location.cell_bbox is not None
        assert all(item.location.cell_bbox[d] >= 0 for d in ("x", "y", "width", "height"))
    assert extracted["product"].raw_document_value == "GLM-4 evaluation unit"
    assert extracted["version"].raw_document_value == "8.0"
    assert extracted["temperature"].raw_document_value == "-10.0 \u00b0C"


def test_value_in_its_own_column_carries_the_value_bbox() -> None:
    extracted = results(device_data_sheet())
    mass = extracted["mass"]
    assert mass.status.value == "extracted"
    assert mass.raw_document_value == "1.25 kg"
    # The evidence points at the printed value, not the label beside it.
    assert mass.location is not None and mass.location.cell_bbox is not None
    assert mass.location.cell_bbox["x"] > 150


def test_compound_text_value_picks_the_component() -> None:
    extracted = results(device_data_sheet())
    assert extracted["accuracy"].status.value == "extracted"
    assert extracted["accuracy"].normalized_document_value == "0.26"
    assert extracted["accuracy_error"].status.value == "extracted"
    assert extracted["accuracy_error"].normalized_document_value == "0.025"


def test_section_labels_resolve_a_repeated_text_label() -> None:
    extracted = results(device_data_sheet())
    current = extracted["revision_current"]
    previous = extracted["revision_previous"]
    assert current.status.value == "extracted"
    assert current.raw_document_value == "B"
    assert current.row_context == "1. Specifications"
    assert previous.status.value == "extracted"
    assert previous.raw_document_value == "A"
    assert previous.row_context == "2. History"


def test_repeated_text_label_without_section_labels_is_ambiguous() -> None:
    raw = json.loads(specification())
    for section in raw["sections"]:
        if section["key"] == "revisions":
            section["fields"] = [field("revision", "Revision", "/revisions/current")]
    plan = SpecificationCompiler().compile(
        json.dumps(raw).encode(), ReferenceSource(reference(), "ref.json")
    )
    extraction = SchemaExtractor().extract(plan, read(device_data_sheet()))
    planned = next(f for f in plan.fields if f.key == "revision")
    revision = next(f for f in extraction.fields if f.field_id == planned.id)
    assert revision.status.value == "ambiguous"


def test_repeated_header_lines_that_agree_resolve() -> None:
    """A header block printed on every page repeats the pair verbatim; the
    copies agree, so the field extracts instead of turning ambiguous."""
    document = pymupdf.open()
    for _ in range(2):
        page = document.new_page(width=612, height=300)
        page.insert_text((48, 60), "Product: GLM-4 evaluation unit", fontsize=10)
    content = document.tobytes()
    document.close()
    extracted = results(content)
    assert extracted["product"].status.value == "extracted"
    assert extracted["product"].raw_document_value == "GLM-4 evaluation unit"


def test_text_label_matches_end_to_end() -> None:
    plan = SpecificationCompiler().compile(
        specification(), ReferenceSource(reference(), "ref.json")
    )
    extraction = SchemaExtractor().extract(plan, read(device_data_sheet()))
    comparisons = ComparisonEngine().compare(plan, extraction)
    assert len(comparisons) == 9
    assert all(item.status is ComparisonStatus.MATCH for item in comparisons)


def test_block_level_evidence_still_pairs() -> None:
    """Evidence written by an older reader holds block-level elements; the
    text pass splits them into lines and keeps pairing."""
    page = PageContent(
        page_number=0,
        width=612,
        text_elements=[
            TextElement(
                text="1. Specifications\nProduct: GLM-4 evaluation unit\nVersion: 8.0",
                confidence=1,
                element_type="text",
                bbox=BoundingBox(x=48, y=84, width=300, height=60, page=0),
            )
        ],
    )
    evidence = DocumentEvidence(
        document_id="legacy", document_hash="h", reader_name="legacy", pages=[page]
    )
    plan = SpecificationCompiler().compile(
        specification(), ReferenceSource(reference(), "ref.json")
    )
    extraction = SchemaExtractor().extract(plan, evidence)
    by_id = {field.id: field.key for field in plan.fields}
    extracted = {by_id[item.field_id]: item for item in extraction.fields}
    assert extracted["product"].status.value == "extracted"
    assert extracted["product"].raw_document_value == "GLM-4 evaluation unit"
    assert extracted["version"].status.value == "extracted"
    assert extracted["version"].raw_document_value == "8.0"


def test_column_labels_are_rejected_for_text_label() -> None:
    raw = json.loads(specification())
    raw["sections"][0]["fields"][0]["locate"]["column_labels"] = ["Value"]
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            json.dumps(raw).encode(), ReferenceSource(reference(), "ref.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"


def test_unknown_strategy_is_rejected() -> None:
    raw = json.loads(specification())
    raw["sections"][0]["fields"][0]["locate"]["strategy"] = "pixel_label"
    with pytest.raises(CompilationError) as caught:
        SpecificationCompiler().compile(
            json.dumps(raw).encode(), ReferenceSource(reference(), "ref.json")
        )
    assert caught.value.diagnostics[0].code == "INVALID_SPECIFICATION_SCHEMA"
