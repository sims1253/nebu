from __future__ import annotations

import hashlib
import json

from janus.comparison.models import DocumentEvidence
from janus.pipeline.review_pipeline import ReviewPipeline
from janus.schemas.review import ReviewMetadata
from janus.store.artifact_store import ReviewArtifactStore
from janus.store.review_store import InMemoryReviewStore


class CountingReader:
    name = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def read(self, _pdf, document_id: str) -> DocumentEvidence:
        self.calls += 1
        return DocumentEvidence(
            document_id=document_id,
            document_hash="document-hash",
            reader_name=self.name,
            pages=[],
        )


class CountingExtractor:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.calls = 0

    def extract(self, plan, evidence):
        self.calls += 1
        return self.delegate.extract(plan, evidence)


async def test_reference_only_revision_reuses_evidence_and_extraction(tmp_path) -> None:
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
                            "reference": {"pointer": "/id"},
                            "value": {"type": "text"},
                            "locate": {"strategy": "table_label", "labels": ["Order number"]},
                            "compare": {"operator": "exact"},
                        }
                    ],
                }
            ],
        }
    ).encode()
    document_path = tmp_path / "document.pdf"
    document_path.write_bytes(b"placeholder")
    specification_path = tmp_path / "specification.json"
    specification_path.write_bytes(specification)
    store = InMemoryReviewStore()
    artifacts = ReviewArtifactStore(results_dir=tmp_path / "artifacts")
    pipeline = ReviewPipeline(store=store, artifacts=artifacts)
    reader = CountingReader()
    pipeline.reader = reader
    extractor = CountingExtractor(pipeline.extractor)
    pipeline.extractor = extractor

    for index, value in enumerate(("A", "B"), start=1):
        reference_path = tmp_path / f"reference-{index}.json"
        reference_content = json.dumps({"id": value}).encode()
        reference_path.write_bytes(reference_content)
        await store.create(
            ReviewMetadata(
                id=f"review-{index}",
                document_filename="document.pdf",
                document_size_bytes=1,
                document_path=str(document_path),
                document_hash="document-hash",
                specification_filename="specification.json",
                specification_size_bytes=len(specification),
                specification_path=str(specification_path),
                specification_id="compiled-later",
                specification_hash=hashlib.sha256(specification).hexdigest(),
                reference_filename=reference_path.name,
                reference_format="json",
                reference_size_bytes=len(reference_content),
                reference_path=str(reference_path),
                reference_hash=hashlib.sha256(reference_content).hexdigest(),
            )
        )
        await pipeline.process(f"review-{index}")

    assert reader.calls == 1
    assert extractor.calls == 1
