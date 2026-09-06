from __future__ import annotations

import hashlib
import json

from janus.comparison.models import DocumentEvidence
from janus.pipeline.review_pipeline import ReviewPipeline
from janus.schemas.review import ReviewMetadata
from janus.store import artifact_store
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


async def test_cache_revision_recomputes_both_stages_but_reference_changes_reuse_them(
    tmp_path, monkeypatch
) -> None:
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

    revisions = [("first", "A", 1), ("first", "B", 1), ("next", "B", 2), ("next", "C", 2)]
    for index, (revision, value, expected_calls) in enumerate(revisions, start=1):
        monkeypatch.setattr(artifact_store, "MACHINE_CACHE_REVISION", revision)
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

        assert reader.calls == expected_calls
        assert extractor.calls == expected_calls

    # Changing the machine cache leaves saved review artifacts readable.
    assert (
        artifacts.load_result("review-1").reference_hash
        != artifacts.load_result("review-4").reference_hash
    )
    assert artifacts.load_evidence("review-1").document_hash == "document-hash"
    assert artifacts.load_extraction("review-1").document_hash == "document-hash"
