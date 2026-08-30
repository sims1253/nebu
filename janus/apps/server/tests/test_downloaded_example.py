from __future__ import annotations

import json
from pathlib import Path

import pytest

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ComparisonStatus
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource

EXAMPLE = Path(__file__).parents[3] / "examples" / "cohort-report"

pytestmark = pytest.mark.skipif(
    not (EXAMPLE / "document.pdf").exists(),
    reason="example document not fetched; run apps/server/scripts/fetch_cohort_example.py",
)


def test_downloaded_example_matches_field_by_field() -> None:
    plan = SpecificationCompiler().compile(
        (EXAMPLE / "specification.json").read_bytes(),
        ReferenceSource.from_path(EXAMPLE / "reference.json"),
    )
    evidence = TextLayerDocumentReader().read(EXAMPLE / "document.pdf", "example")
    comparisons = ComparisonEngine().compare(plan, SchemaExtractor().extract(plan, evidence))

    reference = json.loads((EXAMPLE / "reference.json").read_text())
    expected_fields = sum(
        len(s["fields"])
        for s in json.loads((EXAMPLE / "specification.json").read_text())["sections"]
    )
    assert expected_fields == 10, "the example specification changed size"
    assert len(reference) > 0

    mismatches = [
        f"{item.field_key}: {item.status.value} doc={item.document_value!r}"
        for item in comparisons
        if item.status is not ComparisonStatus.MATCH
    ]
    assert not mismatches, "\n".join(mismatches)
