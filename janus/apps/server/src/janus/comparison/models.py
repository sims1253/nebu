"""Strict domain models for comparison specification v1 and compiled results."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueType(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    PERCENTAGE = "percentage"
    DATE = "date"
    BOOLEAN = "boolean"


class CompoundShape(StrEnum):
    COUNT_PERCENTAGE = "count_percentage"
    MEAN_STANDARD_DEVIATION = "mean_standard_deviation"
    RANGE = "range"
    ESTIMATE_INTERVAL = "estimate_interval"


class ComparisonOperator(StrEnum):
    EXACT = "exact"
    NUMERIC = "numeric"
    DATE = "date"
    BOOLEAN = "boolean"


class Normalizer(StrEnum):
    TRIM = "trim"
    WHITESPACE_COLLAPSE = "whitespace_collapse"
    CASEFOLD = "casefold"
    NUMERIC_PUNCTUATION = "numeric_punctuation"
    CURRENCY_SYMBOL = "currency_symbol"
    PERCENT_SYMBOL = "percent_symbol"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ReferenceBinding(StrictModel):
    pointer: str
    required: bool = True


class ValueRule(StrictModel):
    type: ValueType
    source_shape: CompoundShape | None = None
    component: str | None = None

    @model_validator(mode="after")
    def validate_component(self) -> ValueRule:
        if (self.source_shape is None) != (self.component is None):
            raise ValueError("source_shape and component must be declared together")
        if self.source_shape is not None:
            valid = COMPOUND_COMPONENTS[self.source_shape]
            if self.component not in valid:
                raise ValueError(
                    f"component must be one of {sorted(valid)} for {self.source_shape.value}"
                )
        return self


COMPOUND_COMPONENTS: dict[CompoundShape, frozenset[str]] = {
    CompoundShape.COUNT_PERCENTAGE: frozenset({"count", "percentage"}),
    CompoundShape.MEAN_STANDARD_DEVIATION: frozenset(
        {"mean", "standard_deviation", "p_value", "bayes_factor"}
    ),
    CompoundShape.RANGE: frozenset({"minimum", "maximum"}),
    CompoundShape.ESTIMATE_INTERVAL: frozenset(
        {"estimate", "standard_error", "lower", "upper", "p_value", "bayes_factor"}
    ),
}


def _check_value_pattern(pattern: str | None) -> None:
    """Reject invalid value patterns during compilation."""
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"value_pattern is not a valid regular expression: {exc}") from exc


class TableLabelLocator(StrictModel):
    strategy: Literal["table_label"]
    # Empty only for a rows field, whose labels come from rows.label_pattern;
    # the schema requires labels on every other field.
    labels: list[str] = Field(default_factory=list)
    section_labels: list[str] = Field(default_factory=list)
    column_labels: list[str] = Field(default_factory=list)
    minimum_confidence: float = Field(0.6, ge=0, le=1)
    value_pattern: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_value_pattern(self) -> TableLabelLocator:
        _check_value_pattern(self.value_pattern)
        return self


class TextLabelLocator(StrictModel):
    """A same-baseline label/value pair; column_labels is unsupported."""

    strategy: Literal["text_label"]
    # Empty only for a rows field; see TableLabelLocator.labels.
    labels: list[str] = Field(default_factory=list)
    section_labels: list[str] = Field(default_factory=list)
    minimum_confidence: float = Field(0.6, ge=0, le=1)
    value_pattern: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_value_pattern(self) -> TextLabelLocator:
        _check_value_pattern(self.value_pattern)
        return self


class TextSpanLocator(StrictModel):
    """Prose between a start anchor and an explicit end or the next peer heading."""

    strategy: Literal["text_span"]
    labels: list[str] = Field(min_length=1)
    end_labels: list[str] = Field(default_factory=list)
    section_labels: list[str] = Field(default_factory=list)
    minimum_confidence: float = Field(0.6, ge=0, le=1)
    # A span is prose, so lines inside a geometrically ruled table stay out of
    # it by default; `exclude_tables: false` is the escape hatch that keeps
    # the pre-exclusion behaviour (table cells joined into the prose).
    exclude_tables: bool = True


FieldLocator = Annotated[
    TableLabelLocator | TextLabelLocator | TextSpanLocator, Field(discriminator="strategy")
]


class RowGeneratorRule(StrictModel):
    """One extraction per row whose label matches a pattern.

    Declared on the field (not inside locate) because it replaces the single
    static definition: the compiler cannot know the document, so generation
    happens at extraction time. The pattern selects rows; the capture group
    named by `row_key_from` (or the first group) supplies each row's key
    fragment, sanitized into the field-key charset.
    """

    label_pattern: str = Field(min_length=1, max_length=256)
    max_rows: int = Field(ge=1, le=200)
    row_key_from: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_pattern(self) -> RowGeneratorRule:
        try:
            compiled = re.compile(self.label_pattern)
        except re.error as exc:
            raise ValueError(
                f"rows.label_pattern is not a valid regular expression: {exc}"
            ) from exc
        if compiled.groups < 1:
            raise ValueError("rows.label_pattern must carry a capture group for the row key")
        if self.row_key_from is None or self.row_key_from in compiled.groupindex:
            return self
        if self.row_key_from.isdigit():
            if 1 <= int(self.row_key_from) <= compiled.groups:
                return self
            raise ValueError(
                f"rows.row_key_from {self.row_key_from!r} indexes no capture group; "
                f"the pattern has {compiled.groups}"
            )
        raise ValueError(
            f"rows.row_key_from {self.row_key_from!r} names no capture group in rows.label_pattern"
        )

    def row_key(self, match: re.Match[str]) -> str:
        """The captured key fragment of a matched row label: the named group,
        the indexed group, or the first group when row_key_from is absent."""
        if self.row_key_from is None:
            return match.group(1)
        if self.row_key_from in match.re.groupindex:
            return match.group(self.row_key_from)
        return match.group(int(self.row_key_from))


class ComparisonRule(StrictModel):
    operator: ComparisonOperator
    normalizers: list[Normalizer] = Field(default_factory=list)
    absolute_tolerance: float | None = Field(default=None, ge=0)
    relative_tolerance: float | None = Field(default=None, ge=0)
    severity: Severity = Severity.ERROR

    @model_validator(mode="after")
    def validate_tolerances(self) -> ComparisonRule:
        if self.operator is not ComparisonOperator.NUMERIC and (
            self.absolute_tolerance is not None or self.relative_tolerance is not None
        ):
            raise ValueError("tolerances are only valid with the numeric operator")
        if len(self.normalizers) != len(set(self.normalizers)):
            raise ValueError("normalizers must not contain duplicates")
        return self


class FieldDefinition(StrictModel):
    key: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    label: str = Field(min_length=1)
    reference: ReferenceBinding
    value: ValueRule
    locate: FieldLocator
    compare: ComparisonRule
    rows: RowGeneratorRule | None = None


class SpecificationSection(StrictModel):
    key: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    label: str = Field(min_length=1)
    fields: list[FieldDefinition] = Field(min_length=1)


class ComparisonSpecification(StrictModel):
    schema_uri: str | None = Field(default=None, alias="$schema")
    schema_version: Literal["1"]
    name: str = Field(min_length=1)
    sections: list[SpecificationSection] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_keys(self) -> ComparisonSpecification:
        section_keys = [section.key for section in self.sections]
        if len(section_keys) != len(set(section_keys)):
            raise ValueError("section keys must be unique")
        field_keys: set[str] = set()
        for section in self.sections:
            local = [field.key for field in section.fields]
            if len(local) != len(set(local)):
                raise ValueError(f"field keys in section {section.key!r} must be unique")
            for key in local:
                qualified = f"{section.key}/{key}"
                if qualified in field_keys:
                    raise ValueError(f"duplicate field identity: {qualified}")
                field_keys.add(qualified)
        return self


def field_identifier(
    specification_id: str, section_key: str, key: str, row_key: str | None = None
) -> str:
    """Stable id for a planned field — sha256 over
    `specification_id:section_key:key` — or for one generated row of a rows
    field, which appends the row key as a fourth segment. The row key is
    sanitized to the key charset (no colons), so the segmentation is
    unambiguous and the same document plus specification derives the same ids
    on every run."""
    basis = f"{specification_id}:{section_key}:{key}"
    if row_key is not None:
        basis = f"{basis}:{row_key}"
    return hashlib.sha256(basis.encode()).hexdigest()


class PlannedField(StrictModel):
    id: str
    section_key: str
    section_label: str
    key: str
    label: str
    reference_pointer: str
    reference_required: bool
    expected_value: Any = None
    expected_value_present: bool = True
    value: ValueRule
    locate: FieldLocator
    compare: ComparisonRule
    rows: RowGeneratorRule | None = None


class ComparisonPlan(StrictModel):
    artifact_schema_version: Literal["2"] = "2"
    specification_id: str
    specification_name: str
    specification_version: Literal["1"] = "1"
    reference_format: Literal["json", "csv", "xlsx"]
    reference_hash: str
    extraction_hash: str
    comparison_hash: str
    fields: list[PlannedField]


class EvidenceLocation(StrictModel):
    page: int = Field(ge=0)
    table_index: int = Field(ge=0)
    row: int = Field(ge=0)
    column: int = Field(ge=0)
    raw_text: str
    cell_bbox: dict[str, float] | None = None
    table_bbox: dict[str, float] | None = None
    # A text_span region can cover several pages; these name its last line
    # the way page/row name its first. Optional (like field_key above) so
    # artifacts written before text_span still validate.
    end_page: int | None = Field(default=None, ge=0)
    end_row: int | None = Field(default=None, ge=0)


class DocumentEvidence(StrictModel):
    artifact_schema_version: Literal["2"] = "2"
    document_id: str
    document_hash: str
    reader_name: str
    reader_configuration: dict[str, Any] = Field(default_factory=dict)
    pages: list[Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExtractionStatus(StrEnum):
    EXTRACTED = "extracted"
    NOT_EXTRACTED = "not_extracted"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


class FieldExtraction(StrictModel):
    field_id: str
    status: ExtractionStatus
    # Friendly keys so an extraction artifact reads on its own, without a
    # join through the plan or comparison results. Optional because they
    # arrived after artifact schema version 2 shipped: older artifacts (and
    # machine-written dicts) still validate, with the keys absent.
    field_key: str | None = None
    section_key: str | None = None
    raw_document_value: str | None = None
    normalized_document_value: str | None = None
    location: EvidenceLocation | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    explanation: str
    column_context: str | None = None
    row_context: str | None = None
    # One generated row of a rows field: the derived row key (the captured
    # group, sanitized) and the id of the generating plan field. Both None on
    # ordinary fields. A generator-level note — no label matched the pattern,
    # or the max_rows cap fired — carries row_generator_id with row_key None.
    row_key: str | None = None
    row_generator_id: str | None = None
    # The winning candidate's score breakdown, so tuning a threshold reads the
    # run instead of bisecting blind: the fuzzy label score (None when a rows
    # label pattern selected the candidate), the multiplicative section and
    # column factors, the final score, the configured minimum_confidence, and
    # — on contested outcomes — the runner-up's final score. Optional like
    # field_key above: artifacts written before these fields existed still
    # validate, with the keys absent.
    label_score: float | None = None
    section_factor: float | None = None
    column_factor: float | None = None
    final_score: float | None = None
    minimum_confidence: float | None = None
    runner_up_score: float | None = None
    # Who the runner-up was, not just how it scored: its identifying text
    # (row label, column header, or anchor line) and where it sits. Optional
    # and additive like the score fields above, and populated on the same
    # contested outcomes, so an ambiguous field reads off the artifact
    # without reverse-engineering it through the evidence.
    runner_up_text: str | None = None
    runner_up_location: EvidenceLocation | None = None


class ExtractionResult(StrictModel):
    artifact_schema_version: Literal["2"] = "2"
    specification_id: str
    extraction_hash: str
    document_hash: str
    fields: list[FieldExtraction]


class ComparisonStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_COMPARED = "not_compared"
    AMBIGUOUS = "ambiguous"


class Resolution(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REFERENCE_VALUE_INCORRECT = "reference_value_incorrect"
    INCORRECT_DOCUMENT_MATCH = "incorrect_document_match"
    NOT_PRESENT_IN_DOCUMENT = "not_present_in_document"


class FieldComparison(StrictModel):
    id: str
    field_id: str
    section_key: str
    section_label: str
    field_key: str
    field_label: str
    reference_pointer: str
    reference_value: Any = None
    reference_value_present: bool = True
    extraction_status: ExtractionStatus
    document_value: str | None = None
    normalized_document_value: str | None = None
    document_location: EvidenceLocation | None = None
    column_context: str | None = None
    row_context: str | None = None
    status: ComparisonStatus
    confidence: float = Field(ge=0, le=1)
    severity: Severity | None = None
    explanation: str
    comparator: ComparisonOperator
    normalizers: list[Normalizer]
    absolute_tolerance: float | None = None
    relative_tolerance: float | None = None
    resolution: Resolution = Resolution.PENDING
    notes: str | None = None
    original_document_value: str | None = None
    original_document_location: EvidenceLocation | None = None


class ReviewSummary(StrictModel):
    total_fields: int = 0
    matched: int = 0
    mismatched: int = 0
    not_compared: int = 0
    ambiguous: int = 0
    coverage_rate: float = 0
    # None when no field was actually compared (avoids a vacuous 100%).
    agreement_rate: float | None = None
    by_severity: dict[str, int] = Field(default_factory=dict)


class ReviewResult(StrictModel):
    artifact_schema_version: Literal["2"] = "2"
    review_id: str
    specification_id: str
    reference_hash: str
    extraction_hash: str
    comparisons: list[FieldComparison]
    summary: ReviewSummary
    processing_time_seconds: float = Field(ge=0)


class CompilationDiagnostic(StrictModel):
    code: str
    specification_path: str
    message: str
    remediation_hint: str
    reference_pointer: str | None = None


class CompilationError(ValueError):
    def __init__(self, diagnostics: list[CompilationDiagnostic]) -> None:
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.message for item in diagnostics))
