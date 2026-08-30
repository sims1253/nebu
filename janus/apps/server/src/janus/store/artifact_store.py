"""Persistence for review artifacts.

Each review gets a directory holding its plan, evidence, extraction, result,
and progress as versioned JSON files. Two shared caches live beside them:
evidence keyed by document hash, extractions keyed by document hash plus
extraction hash. Writes are atomic (temp file, fsync, rename), so a crash
never leaves a half-written artifact.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from janus.comparison.engine import ComparisonEngine
from janus.comparison.models import (
    ComparisonPlan,
    DocumentEvidence,
    EvidenceLocation,
    ExtractionResult,
    FieldComparison,
    Resolution,
    ReviewResult,
)
from janus.config import artifact_root
from janus.schemas.review import ReviewProgress

T = TypeVar("T", bound=BaseModel)
ARTIFACT_SCHEMA_VERSION = "2"


class ArtifactVersionError(ValueError):
    pass


class ReviewArtifactStore:
    def __init__(self, *, results_dir: Path | None = None) -> None:
        self._results_dir = results_dir or artifact_root()

    @property
    def results_dir(self) -> Path:
        return self._results_dir

    def _review_dir(self, review_id: str) -> Path:
        return self._results_dir / "reviews" / review_id

    def _path(self, review_id: str, name: str) -> Path:
        return self._review_dir(review_id) / f"{name}.v2.json"

    def _cache_path(self, kind: str, key: str) -> Path:
        return self._results_dir / "cache" / kind / f"{key}.v2.json"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _save(self, review_id: str, name: str, value: BaseModel) -> None:
        self._atomic_write(self._path(review_id, name), value.model_dump_json(indent=2))

    def _load(self, review_id: str, name: str, model: type[T]) -> T:
        data = json.loads(self._path(review_id, name).read_text(encoding="utf-8"))
        if data.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactVersionError(
                f"unsupported {name} artifact version: {data.get('artifact_schema_version')!r}"
            )
        return model.model_validate(data)

    def save_plan(self, review_id: str, value: ComparisonPlan) -> None:
        self._save(review_id, "plan", value)

    def load_plan(self, review_id: str) -> ComparisonPlan:
        return self._load(review_id, "plan", ComparisonPlan)

    def save_evidence(self, review_id: str, value: DocumentEvidence) -> None:
        self._save(review_id, "evidence", value)
        self._atomic_write(
            self._cache_path("evidence", value.document_hash),
            value.model_dump_json(indent=2),
        )

    def load_evidence(self, review_id: str) -> DocumentEvidence:
        return self._load(review_id, "evidence", DocumentEvidence)

    def load_cached_evidence(self, document_hash: str) -> DocumentEvidence:
        path = self._cache_path("evidence", document_hash)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactVersionError("unsupported evidence cache version")
        return DocumentEvidence.model_validate(data)

    def save_extraction(self, review_id: str, value: ExtractionResult) -> None:
        self._save(review_id, "extraction", value)
        cache_key = f"{value.document_hash}-{value.extraction_hash}"
        self._atomic_write(
            self._cache_path("extraction", cache_key),
            value.model_dump_json(indent=2),
        )

    def load_extraction(self, review_id: str) -> ExtractionResult:
        return self._load(review_id, "extraction", ExtractionResult)

    def save_review_extraction(self, review_id: str, value: ExtractionResult) -> None:
        """Persist a per-review extraction without touching the shared cache.

        Reviewer corrections amend the review's own extraction artifact; the
        document-hash-keyed cache must keep holding the machine extraction.
        """
        self._save(review_id, "extraction", value)

    def load_cached_extraction(self, document_hash: str, extraction_hash: str) -> ExtractionResult:
        path = self._cache_path("extraction", f"{document_hash}-{extraction_hash}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("artifact_schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactVersionError("unsupported extraction cache version")
        return ExtractionResult.model_validate(data)

    def save_result(self, review_id: str, value: ReviewResult) -> None:
        self._save(review_id, "result", value)

    def load_result(self, review_id: str) -> ReviewResult:
        return self._load(review_id, "result", ReviewResult)

    def save_progress(self, review_id: str, value: ReviewProgress) -> None:
        self._save(review_id, "progress", value)

    def load_progress(self, review_id: str) -> ReviewProgress:
        return self._load(review_id, "progress", ReviewProgress)

    def annotate_comparison(
        self,
        *,
        review_id: str,
        comparison_id: str,
        resolution: Resolution,
        notes: str | None,
        document_value: str | None,
    ) -> FieldComparison | None:
        """Record a reviewer's decision on one comparison and persist the
        updated result.

        A corrected document_value stores the original value, amends the
        review's own extraction (never the shared cache), and recomputes that
        field's status, severity, and explanation through the engine. Returns
        None when the comparison id is unknown.
        """
        result = self.load_result(review_id)
        for comparison in result.comparisons:
            if comparison.id != comparison_id:
                continue
            comparison.resolution = resolution
            comparison.notes = notes
            if document_value is not None and document_value != comparison.document_value:
                if comparison.original_document_value is None:
                    comparison.original_document_value = comparison.document_value
                plan = self.load_plan(review_id)
                extraction = self.load_extraction(review_id).model_copy(deep=True)
                extracted = next(
                    (item for item in extraction.fields if item.field_id == comparison.field_id),
                    None,
                )
                if extracted is not None:
                    extracted.raw_document_value = document_value
                    extracted.normalized_document_value = document_value
                    extracted.confidence = 1.0
                    refreshed = ComparisonEngine().compare(plan, extraction)
                    updated = next(item for item in refreshed if item.id == comparison.id)
                    self.save_review_extraction(review_id, extraction)
                    comparison.document_value = document_value
                    comparison.normalized_document_value = document_value
                    comparison.status = updated.status
                    comparison.severity = updated.severity
                    comparison.extraction_status = updated.extraction_status
                    comparison.confidence = 1.0
                    comparison.explanation = (
                        f"Recomputed after reviewer correction. {updated.explanation}"
                    )
            result.summary = ComparisonEngine.summarize(result.comparisons)
            self.save_result(review_id, result)
            return comparison
        return None

    def update_location(
        self,
        *,
        review_id: str,
        comparison_id: str,
        location: EvidenceLocation,
        notes: str | None,
    ) -> FieldComparison | None:
        """Overwrite a comparison's document location with a reviewer-supplied
        one, keeping the machine location in original_document_location.
        Returns None when the comparison id is unknown."""
        result = self.load_result(review_id)
        for comparison in result.comparisons:
            if comparison.id != comparison_id:
                continue
            if comparison.original_document_location is None:
                comparison.original_document_location = comparison.document_location
            comparison.document_location = location
            if notes is not None:
                comparison.notes = notes
            self.save_result(review_id, result)
            return comparison
        return None

    def delete_review_artifacts(self, review_id: str) -> list[str]:
        directory = self._review_dir(review_id)
        if not directory.exists():
            return []
        removed: list[str] = []
        for path in directory.iterdir():
            if path.is_file():
                path.unlink()
                removed.append(str(path))
        directory.rmdir()
        return removed


_artifact_store: ReviewArtifactStore | None = None


def get_artifact_store() -> ReviewArtifactStore:
    global _artifact_store
    if _artifact_store is None:
        root = os.getenv("JANUS_ARTIFACT_DIR")
        _artifact_store = ReviewArtifactStore(results_dir=Path(root) if root else None)
    return _artifact_store


# Test hooks, same pattern as review_store.
def reset_artifact_store() -> None:
    global _artifact_store
    _artifact_store = None


def set_artifact_store(store: ReviewArtifactStore | None) -> None:
    global _artifact_store
    _artifact_store = store
