"""Runs one review through the four comparison stages: compile, read,
extract, compare. Each stage persists an artifact before the next begins."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.engine import ComparisonEngine
from janus.comparison.extractor import SchemaExtractor
from janus.comparison.models import ReviewResult
from janus.comparison.reader import TextLayerDocumentReader
from janus.comparison.reference import ReferenceSource
from janus.schemas.review import ReviewProgress, ReviewStatus
from janus.store.artifact_store import ReviewArtifactStore, get_artifact_store
from janus.store.review_store import ReviewStore, get_review_store


class ReviewPipeline:
    def __init__(
        self,
        *,
        store: ReviewStore | None = None,
        artifacts: ReviewArtifactStore | None = None,
    ) -> None:
        self.store = store or get_review_store()
        self.artifacts = artifacts or get_artifact_store()
        self.compiler = SpecificationCompiler()
        self.reader = TextLayerDocumentReader()
        self.extractor = SchemaExtractor()
        self.comparison = ComparisonEngine()

    async def _progress(
        self, review_id: str, status: ReviewStatus, percent: float, detail: str
    ) -> None:
        progress = ReviewProgress(
            review_id=review_id,
            status=status,
            progress_percent=percent,
            current_stage_detail=detail,
        )
        await asyncio.to_thread(self.artifacts.save_progress, review_id, progress)

    async def process(self, review_id: str) -> None:
        """Run the full pipeline for one stored review.

        Raises KeyError when the review id is unknown. On any other failure
        the review is set to ERROR and the exception is re-raised.
        """
        metadata = await self.store.get(review_id)
        if metadata is None:
            raise KeyError(f"review not found: {review_id}")
        started = time.monotonic()
        # Compile, read, extract, and compare are CPU- and IO-bound work; run
        # them off the event loop so other requests stay responsive.
        try:
            await self.store.update_status(review_id, ReviewStatus.VALIDATING_INPUTS)
            await self._progress(review_id, ReviewStatus.VALIDATING_INPUTS, 10, "Compiling inputs")

            def compile_plan():
                return self.compiler.compile(
                    Path(metadata.specification_path).read_bytes(),
                    ReferenceSource.from_path(Path(metadata.reference_path)),
                )

            plan = await asyncio.to_thread(compile_plan)
            await asyncio.to_thread(self.artifacts.save_plan, review_id, plan)

            await self.store.update_status(review_id, ReviewStatus.READING_DOCUMENT)
            await self._progress(
                review_id, ReviewStatus.READING_DOCUMENT, 30, "Reading source document"
            )
            # FileNotFoundError: no cache entry yet. ValueError: the cached
            # artifact has an unsupported schema version (ArtifactVersionError
            # subclasses ValueError). Either way, fall back to a fresh read.
            try:
                evidence = await asyncio.to_thread(
                    self.artifacts.load_cached_evidence, metadata.document_hash
                )
            except (FileNotFoundError, ValueError):
                evidence = await asyncio.to_thread(
                    self.reader.read, Path(metadata.document_path), metadata.document_hash
                )
            await asyncio.to_thread(self.artifacts.save_evidence, review_id, evidence)

            await self.store.update_status(review_id, ReviewStatus.EXTRACTING_FIELDS)
            await self._progress(review_id, ReviewStatus.EXTRACTING_FIELDS, 60, "Extracting fields")
            # Same cache contract as above: on a miss or a stale version, redo
            # the work.
            try:
                extraction = await asyncio.to_thread(
                    self.artifacts.load_cached_extraction,
                    evidence.document_hash,
                    plan.extraction_hash,
                )
            except (FileNotFoundError, ValueError):
                extraction = await asyncio.to_thread(self.extractor.extract, plan, evidence)
            await asyncio.to_thread(self.artifacts.save_extraction, review_id, extraction)

            await self.store.update_status(review_id, ReviewStatus.COMPARING)
            await self._progress(review_id, ReviewStatus.COMPARING, 85, "Comparing values")
            comparisons = await asyncio.to_thread(self.comparison.compare, plan, extraction)
            result = ReviewResult(
                review_id=review_id,
                specification_id=plan.specification_id,
                reference_hash=plan.reference_hash,
                extraction_hash=plan.extraction_hash,
                comparisons=comparisons,
                summary=self.comparison.summarize(comparisons),
                processing_time_seconds=time.monotonic() - started,
            )
            await asyncio.to_thread(self.artifacts.save_result, review_id, result)
            await self.store.update_status(review_id, ReviewStatus.READY)
            await self._progress(review_id, ReviewStatus.READY, 100, "Review ready")
        except Exception as exc:
            current = await self.store.get(review_id)
            if current is not None and current.status is not ReviewStatus.ERROR:
                await self.store.update_status(
                    review_id, ReviewStatus.ERROR, f"{type(exc).__name__}: {exc}"
                )
            await self._progress(review_id, ReviewStatus.ERROR, 100, "Review failed")
            raise


def get_review_pipeline() -> ReviewPipeline:
    """Build a ReviewPipeline over the stores selected by the environment."""
    return ReviewPipeline()
