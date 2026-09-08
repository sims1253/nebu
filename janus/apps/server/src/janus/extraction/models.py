"""Extraction contracts. No reference data or comparison rules are required."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from janus.comparison.models import EvidenceLocation, FieldLocator, StrictModel, ValueRule


class Scalar(ValueRule):
    decimal_separator: Literal[".", ","] = "."
    group_separator: Literal[".", ",", " "] | None = None
    strip_prefix: str = ""
    strip_suffix: str = ""

    @model_validator(mode="after")
    def distinct_separators(self) -> Scalar:
        if self.decimal_separator == self.group_separator:
            raise ValueError("Decimal and group separators must differ.")
        return self


class ScalarField(Scalar):
    locate: FieldLocator

    @model_validator(mode="after")
    def needs_labels(self) -> ScalarField:
        if not self.locate.labels:
            raise ValueError("A scalar field needs at least one locator label.")
        return self


class RecordColumn(Scalar):
    labels: list[str] = Field(min_length=1)


class RecordsField(StrictModel):
    type: Literal["records"]
    columns: dict[str, RecordColumn] = Field(min_length=1)
    max_rows: int = Field(default=200, ge=1, le=2000)


class ObjectField(StrictModel):
    type: Literal["object"]
    fields: dict[str, OutputField] = Field(min_length=1)


# Scalars share ValueRule's type enum; union validation distinguishes objects/records.
OutputField = ScalarField | ObjectField | RecordsField
ObjectField.model_rebuild()


class ExtractionSpecification(StrictModel):
    kind: Literal["extraction"] = "extraction"
    schema_version: Literal["1"] = "1"
    name: str = Field(min_length=1, max_length=200)
    fields: dict[str, OutputField] = Field(min_length=1)


class FieldResult(StrictModel):
    path: str
    type: str
    status: Literal["extracted", "missing", "ambiguous", "invalid", "error"]
    source: EvidenceLocation | None = None
    message: str = ""


class PageSize(StrictModel):
    width: float
    height: float


class StructuredExtraction(StrictModel):
    schema_version: Literal["1"] = "1"
    specification_name: str
    specification_hash: str
    document_hash: str
    data: dict[str, Any]
    fields: list[FieldResult]
    pages: list[PageSize]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SavedExtraction(StrictModel):
    id: str
    document_filename: str
    result: StructuredExtraction


class ExtractionSummary(StrictModel):
    id: str
    document_filename: str
    specification_name: str
    created_at: datetime
    fields: int
    issues: int


class CheckRule(StrictModel):
    path: str
    reference_pointer: str
    absolute_tolerance: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0


class CheckResult(StrictModel):
    path: str
    reference_pointer: str
    status: Literal["match", "mismatch", "not_compared"]
    actual: Any = None
    expected: Any = None
    message: str
