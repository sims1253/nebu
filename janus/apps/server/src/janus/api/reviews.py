"""HTTP API under /api/reviews: validate inputs, create reviews, track
progress, read results, record reviewer decisions and corrected locations,
and delete reviews."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Literal, NoReturn, cast

import pymupdf
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from janus.comparison.compiler import SpecificationCompiler
from janus.comparison.models import (
    CompilationDiagnostic,
    CompilationError,
    EvidenceLocation,
    Resolution,
    ReviewResult,
)
from janus.comparison.reference import ReferenceSource
from janus.config import upload_root
from janus.pipeline.review_pipeline import get_review_pipeline
from janus.schemas.review import ReviewMetadata, ReviewProgress, ReviewStatus
from janus.store.artifact_store import ArtifactVersionError, get_artifact_store
from janus.store.review_store import get_review_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reviews", tags=["reviews"])
# Strong references keep fire-and-forget pipeline tasks alive until they
# finish; asyncio only holds weak references to running tasks.
_background_tasks: set[asyncio.Task[None]] = set()
_MAX_DOCUMENT_SIZE = 500 * 1024 * 1024
_MAX_INPUT_SIZE = 50 * 1024 * 1024
# Review ids are uuid4().hex; validate before they reach any path operation.
_REVIEW_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiErrorDetail(BaseModel):
    code: str
    message: str
    hint: str | None = None
    diagnostics: list[CompilationDiagnostic] = Field(default_factory=list)


class ValidationResponse(BaseModel):
    valid: bool = True
    specification_id: str
    specification_version: str
    reference_format: str
    field_count: int


class AnnotationRequest(StrictRequest):
    comparison_id: str
    resolution: Resolution
    notes: str | None = None
    document_value: str | None = None


class AnnotationResponse(BaseModel):
    comparison_id: str
    resolution: Resolution
    notes: str | None = None
    document_value: str | None = None
    original_document_value: str | None = None


class LocationHintRequest(StrictRequest):
    comparison_id: str
    page: int = Field(ge=0)
    table_index: int = Field(default=0, ge=0)
    row: int = Field(default=0, ge=0)
    column: int = Field(default=0, ge=0)
    cell_bbox: dict[str, float]
    raw_text: str = ""
    notes: str | None = None


class LocationHintResponse(BaseModel):
    comparison_id: str
    page: int
    persisted: bool


class ReviewDeleteResponse(BaseModel):
    review_id: str
    removed_entries: list[str]


def _error(
    status_code: int,
    code: str,
    message: str,
    hint: str | None = None,
    diagnostics: list[CompilationDiagnostic] | None = None,
) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail=ApiErrorDetail(
            code=code,
            message=message,
            hint=hint,
            diagnostics=diagnostics or [],
        ).model_dump(mode="json"),
    )


def _validate_review_id(review_id: str) -> None:
    if not _REVIEW_ID_PATTERN.fullmatch(review_id):
        _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_REVIEW_ID",
            "Review id must be a 32-character hexadecimal identifier.",
        )


_UPLOAD_CHUNK_SIZE = 1024 * 1024


def _check_extension(filename: str, allowed: set[str], kind: str) -> str:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in allowed:
        _error(
            status.HTTP_400_BAD_REQUEST,
            f"INVALID_{kind.upper()}_FORMAT",
            f"Unsupported {kind} format.",
            f"Use one of: {', '.join(sorted(allowed))}.",
        )
    return extension


async def _read_upload(
    upload: UploadFile, *, allowed: set[str], limit: int, kind: str
) -> tuple[str, str, bytes]:
    filename = upload.filename or ""
    extension = _check_extension(filename, allowed, kind)
    # Read in chunks so an oversized input is rejected mid-stream instead of
    # being buffered fully into memory first.
    content = bytearray()
    while chunk := await upload.read(_UPLOAD_CHUNK_SIZE):
        content.extend(chunk)
        if len(content) > limit:
            _error(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"{kind.upper()}_TOO_LARGE",
                f"The {kind} exceeds the configured size limit.",
            )
    if not content:
        _error(status.HTTP_400_BAD_REQUEST, f"EMPTY_{kind.upper()}", f"The {kind} is empty.")
    return filename, extension, bytes(content)


async def _store_document(upload: UploadFile, destination: Path) -> tuple[int, str]:
    """Stream a PDF to disk under the size limit; return its size and SHA-256."""
    digest = hashlib.sha256()
    total = 0
    header = b""
    with destination.open("wb") as stream:
        while chunk := await upload.read(_UPLOAD_CHUNK_SIZE):
            if not header:
                header = bytes(chunk[:5])
            total += len(chunk)
            if total > _MAX_DOCUMENT_SIZE:
                _error(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "DOCUMENT_TOO_LARGE",
                    "The document exceeds the configured size limit.",
                )
            digest.update(chunk)
            await asyncio.to_thread(stream.write, chunk)
    if total == 0:
        _error(status.HTTP_400_BAD_REQUEST, "EMPTY_DOCUMENT", "The document is empty.")
    if not header.startswith(b"%PDF-"):
        _error(
            status.HTTP_400_BAD_REQUEST,
            "INVALID_DOCUMENT_CONTENT",
            "The source document is not a valid PDF.",
            "Upload a PDF with a valid file header.",
        )
    return total, digest.hexdigest()


def _compile_or_error(specification: bytes, reference: bytes, reference_name: str):
    try:
        return SpecificationCompiler().compile(
            specification, ReferenceSource(reference, reference_name)
        )
    except CompilationError as exc:
        _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "INPUT_COMPILATION_FAILED",
            "The comparison specification and reference dataset are not compatible.",
            "Review the structured diagnostics and correct the inputs.",
            exc.diagnostics,
        )


@router.post("/validate", response_model=ValidationResponse, summary="Validate review inputs")
async def validate_inputs(
    specification: UploadFile = File(...), reference: UploadFile = File(...)
) -> ValidationResponse:
    _, _, specification_content = await _read_upload(
        specification, allowed={"json"}, limit=_MAX_INPUT_SIZE, kind="specification"
    )
    reference_name, reference_format, reference_content = await _read_upload(
        reference, allowed={"json", "csv", "xlsx"}, limit=_MAX_INPUT_SIZE, kind="reference"
    )
    plan = _compile_or_error(specification_content, reference_content, reference_name)
    return ValidationResponse(
        specification_id=plan.specification_id,
        specification_version=plan.specification_version,
        reference_format=reference_format,
        field_count=len(plan.fields),
    )


@router.post("", response_model=ReviewMetadata, status_code=status.HTTP_201_CREATED)
async def create_review(
    document: UploadFile = File(...),
    specification: UploadFile = File(...),
    reference: UploadFile = File(...),
) -> ReviewMetadata:
    document_name = document.filename or ""
    _check_extension(document_name, {"pdf"}, "document")
    specification_name, _, specification_content = await _read_upload(
        specification, allowed={"json"}, limit=_MAX_INPUT_SIZE, kind="specification"
    )
    reference_name, reference_format, reference_content = await _read_upload(
        reference, allowed={"json", "csv", "xlsx"}, limit=_MAX_INPUT_SIZE, kind="reference"
    )

    review_id = uuid.uuid4().hex
    review_dir = upload_root() / review_id
    try:
        review_dir.mkdir(parents=True, exist_ok=False)
        document_path = review_dir / "document.pdf"
        document_size, document_hash = await _store_document(document, document_path)

        def validate_pdf() -> None:
            with pymupdf.open(document_path) as parsed_document:
                if parsed_document.page_count < 1:
                    raise ValueError("PDF contains no pages")

        try:
            await asyncio.to_thread(validate_pdf)
        except (pymupdf.FileDataError, ValueError) as exc:
            _error(
                status.HTTP_400_BAD_REQUEST,
                "INVALID_DOCUMENT_CONTENT",
                f"The source document cannot be read: {exc}",
                "Upload a complete, uncorrupted PDF.",
            )
        plan = _compile_or_error(specification_content, reference_content, reference_name)

        specification_path = review_dir / "specification.json"
        reference_path = review_dir / f"reference.{reference_format}"
        await asyncio.to_thread(specification_path.write_bytes, specification_content)
        await asyncio.to_thread(reference_path.write_bytes, reference_content)
        metadata = ReviewMetadata(
            id=review_id,
            document_filename=document_name,
            document_size_bytes=document_size,
            document_path=str(document_path),
            document_hash=document_hash,
            specification_filename=specification_name,
            specification_size_bytes=len(specification_content),
            specification_path=str(specification_path),
            specification_id=plan.specification_id,
            specification_hash=hashlib.sha256(specification_content).hexdigest(),
            reference_filename=reference_name,
            reference_format=cast("Literal['json', 'csv', 'xlsx']", reference_format),
            reference_size_bytes=len(reference_content),
            reference_path=str(reference_path),
            reference_hash=plan.reference_hash,
        )
        await get_review_store().create(metadata)
    except Exception:
        shutil.rmtree(review_dir, ignore_errors=True)
        raise

    task = asyncio.create_task(_run_pipeline(review_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return metadata


async def _run_pipeline(review_id: str) -> None:
    try:
        await get_review_pipeline().process(review_id)
    except Exception:
        # The pipeline normally persists a safe error state itself; log in case
        # it failed before doing so, and never leak the exception out of the task.
        logger.exception("review pipeline failed for review %s", review_id)


@router.get("", response_model=list[ReviewMetadata])
async def list_reviews() -> list[ReviewMetadata]:
    return await get_review_store().list_all()


@router.get("/{review_id}", response_model=ReviewMetadata)
async def get_review(review_id: str) -> ReviewMetadata:
    _validate_review_id(review_id)
    metadata = await get_review_store().get(review_id)
    if metadata is None:
        _error(status.HTTP_404_NOT_FOUND, "REVIEW_NOT_FOUND", "Review not found.")
    return metadata


@router.get("/{review_id}/progress", response_model=ReviewProgress)
async def get_review_progress(review_id: str) -> ReviewProgress:
    _validate_review_id(review_id)
    if await get_review_store().get(review_id) is None:
        _error(status.HTTP_404_NOT_FOUND, "REVIEW_NOT_FOUND", "Review not found.")
    try:
        return get_artifact_store().load_progress(review_id)
    except FileNotFoundError:
        return ReviewProgress(review_id=review_id, status=ReviewStatus.CREATED)
    except ArtifactVersionError as exc:
        _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "ARTIFACT_VERSION_UNSUPPORTED",
            f"Stored progress artifact cannot be read: {exc}",
        )


@router.get("/{review_id}/result", response_model=ReviewResult)
async def get_review_result(review_id: str) -> ReviewResult:
    _validate_review_id(review_id)
    metadata = await get_review_store().get(review_id)
    if metadata is None:
        _error(status.HTTP_404_NOT_FOUND, "REVIEW_NOT_FOUND", "Review not found.")
    if metadata.status is not ReviewStatus.READY:
        _error(status.HTTP_409_CONFLICT, "REVIEW_NOT_READY", "Review is not ready.")
    try:
        return get_artifact_store().load_result(review_id)
    except FileNotFoundError:
        _error(status.HTTP_404_NOT_FOUND, "RESULT_NOT_FOUND", "Review result not found.")
    except ArtifactVersionError as exc:
        _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "ARTIFACT_VERSION_UNSUPPORTED",
            f"Stored result artifact cannot be read: {exc}",
        )


@router.get("/{review_id}/document", response_class=FileResponse)
async def get_review_document(review_id: str) -> FileResponse:
    _validate_review_id(review_id)
    metadata = await get_review_store().get(review_id)
    if metadata is None:
        _error(status.HTTP_404_NOT_FOUND, "REVIEW_NOT_FOUND", "Review not found.")
    return FileResponse(
        metadata.document_path,
        media_type="application/pdf",
        filename=metadata.document_filename,
    )


@router.post("/{review_id}/annotations", response_model=AnnotationResponse)
async def annotate_review(review_id: str, request: AnnotationRequest) -> AnnotationResponse:
    _validate_review_id(review_id)
    try:
        comparison = get_artifact_store().annotate_comparison(
            review_id=review_id,
            comparison_id=request.comparison_id,
            resolution=request.resolution,
            notes=request.notes if "notes" in request.model_fields_set else ...,
            document_value=request.document_value,
        )
    except FileNotFoundError:
        _error(status.HTTP_404_NOT_FOUND, "RESULT_NOT_FOUND", "Review result not found.")
    except ArtifactVersionError as exc:
        _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "ARTIFACT_VERSION_UNSUPPORTED",
            f"Stored artifact cannot be read: {exc}",
        )
    if comparison is None:
        _error(status.HTTP_404_NOT_FOUND, "COMPARISON_NOT_FOUND", "Field comparison not found.")
    return AnnotationResponse(
        comparison_id=comparison.id,
        resolution=comparison.resolution,
        notes=comparison.notes,
        document_value=comparison.document_value,
        original_document_value=comparison.original_document_value,
    )


@router.post("/{review_id}/location-hints", response_model=LocationHintResponse)
async def save_location_hint(review_id: str, request: LocationHintRequest) -> LocationHintResponse:
    _validate_review_id(review_id)
    location = EvidenceLocation(
        page=request.page,
        table_index=request.table_index,
        row=request.row,
        column=request.column,
        raw_text=request.raw_text,
        cell_bbox=request.cell_bbox,
    )
    try:
        comparison = get_artifact_store().update_location(
            review_id=review_id,
            comparison_id=request.comparison_id,
            location=location,
            notes=request.notes,
        )
    except FileNotFoundError:
        _error(status.HTTP_404_NOT_FOUND, "RESULT_NOT_FOUND", "Review result not found.")
    except ArtifactVersionError as exc:
        _error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "ARTIFACT_VERSION_UNSUPPORTED",
            f"Stored artifact cannot be read: {exc}",
        )
    if comparison is None:
        _error(status.HTTP_404_NOT_FOUND, "COMPARISON_NOT_FOUND", "Field comparison not found.")
    return LocationHintResponse(comparison_id=comparison.id, page=request.page, persisted=True)


@router.delete("/{review_id}", response_model=ReviewDeleteResponse)
async def delete_review(review_id: str) -> ReviewDeleteResponse:
    _validate_review_id(review_id)
    metadata = await get_review_store().get(review_id)
    removed = get_artifact_store().delete_review_artifacts(review_id)
    existed = await get_review_store().delete(review_id)
    if metadata is not None:
        directory = Path(metadata.document_path).parent
        shutil.rmtree(directory, ignore_errors=True)
        removed.append(str(directory))
    if not existed and not removed:
        _error(status.HTTP_404_NOT_FOUND, "REVIEW_NOT_FOUND", "Review not found.")
    return ReviewDeleteResponse(review_id=review_id, removed_entries=removed)
