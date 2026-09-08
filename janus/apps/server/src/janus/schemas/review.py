"""Wire models for review metadata (artifact schema v2)."""

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


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "ReviewMetadata",
    "ReviewResult",
    "ReviewStatus",
]
