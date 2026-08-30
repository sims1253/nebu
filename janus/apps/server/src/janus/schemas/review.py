"""Wire models for review metadata and progress (artifact schema v2)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from janus.comparison.models import ReviewResult

ARTIFACT_SCHEMA_VERSION = "2"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewStatus(StrEnum):
    CREATED = "created"
    VALIDATING_INPUTS = "validating_inputs"
    READING_DOCUMENT = "reading_document"
    EXTRACTING_FIELDS = "extracting_fields"
    COMPARING = "comparing"
    READY = "ready"
    ERROR = "error"


class ReviewMetadata(StrictModel):
    artifact_schema_version: Literal["2"] = "2"
    id: str
    document_filename: str
    document_format: Literal["pdf"] = "pdf"
    document_size_bytes: int = Field(ge=0)
    document_path: str
    document_hash: str
    specification_filename: str
    specification_size_bytes: int = Field(ge=0)
    specification_path: str
    specification_id: str
    specification_version: Literal["1"] = "1"
    specification_hash: str
    reference_filename: str
    reference_format: Literal["json", "csv", "xlsx"]
    reference_size_bytes: int = Field(ge=0)
    reference_path: str
    reference_hash: str
    status: ReviewStatus = ReviewStatus.CREATED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_message: str | None = None


class ReviewProgress(StrictModel):
    artifact_schema_version: Literal["2"] = "2"
    review_id: str
    status: ReviewStatus
    pages_processed: int = Field(default=0, ge=0)
    total_pages: int = Field(default=0, ge=0)
    progress_percent: float = Field(default=0, ge=0, le=100)
    current_stage_detail: str | None = None
    estimated_remaining_seconds: float | None = Field(default=None, ge=0)


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ReviewMetadata",
    "ReviewProgress",
    "ReviewResult",
    "ReviewStatus",
]
