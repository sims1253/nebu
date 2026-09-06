"""Compares extracted values against the values the plan expects.

The engine reads only the plan and the extraction; it never touches the
source document, so comparisons run and test without a PDF."""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal, DecimalException
from typing import Any

from janus.comparison.models import (
    ComparisonOperator,
    ComparisonPlan,
    ComparisonStatus,
    ExtractionResult,
    ExtractionStatus,
    FieldComparison,
    FieldExtraction,
    Normalizer,
    PlannedField,
    ReviewSummary,
    field_identifier,
)


def _normalize(value: Any, normalizers: list[Normalizer]) -> str:
    result = "" if value is None else str(value)
    for normalizer in normalizers:
        if normalizer is Normalizer.TRIM:
            result = result.strip()
        elif normalizer is Normalizer.WHITESPACE_COLLAPSE:
            result = " ".join(result.split())
        elif normalizer is Normalizer.CASEFOLD:
            result = result.casefold()
        elif normalizer is Normalizer.NUMERIC_PUNCTUATION:
            # A lone separator is decimal: "1,234" becomes "1.234".
            # Matching conventions on both sides cannot resolve locale ambiguity.
            compact = result.replace(" ", "").replace("\u00a0", "")
            last_comma = compact.rfind(",")
            last_dot = compact.rfind(".")
            if last_comma >= 0 and last_dot >= 0:
                if last_comma > last_dot:
                    compact = compact.replace(".", "").replace(",", ".")
                else:
                    compact = compact.replace(",", "")
            elif last_comma >= 0:
                if compact.count(",") > 1:
                    compact = compact.replace(",", "")
                else:
                    compact = compact.replace(",", ".")
            elif last_dot >= 0 and compact.count(".") > 1:
                compact = compact.replace(".", "")
            result = compact
        elif normalizer is Normalizer.CURRENCY_SYMBOL:
            # Currency codes such as USD and EUR remain unsupported.
            result = result.strip().lstrip("$€£¥").strip()
        elif normalizer is Normalizer.PERCENT_SYMBOL:
            result = result.replace("%", "").strip()
    return result


def _as_bool(value: str) -> bool | None:
    normalized = value.strip().casefold()
    if normalized in {"true", "yes", "1"}:
        return True
    if normalized in {"false", "no", "0"}:
        return False
    return None


def _compare(
    expected: str,
    actual: str,
    operator: ComparisonOperator,
    absolute_tolerance: float | None,
    relative_tolerance: float | None,
) -> tuple[bool, str]:
    if operator is ComparisonOperator.EXACT:
        return expected == actual, "Compared normalized text for exact equality."
    if operator is ComparisonOperator.BOOLEAN:
        expected_bool, actual_bool = _as_bool(expected), _as_bool(actual)
        if expected_bool is None or actual_bool is None:
            return False, "One value is not a recognized boolean."
        return expected_bool == actual_bool, "Compared canonical boolean values."
    if operator is ComparisonOperator.DATE:
        # Each side parses independently: ISO-8601 first, then dotted
        # day-first (02.09.2026), which European documents commonly print.
        # Slashed month/day forms stay unsupported; they are ambiguous
        # without a locale.
        def parse(value: str) -> date | None:
            for parser in (
                date.fromisoformat,
                lambda text: datetime.strptime(text, "%d.%m.%Y").date(),
            ):
                try:
                    return parser(value)
                except ValueError:
                    continue
            return None

        expected_date, actual_date = parse(expected), parse(actual)
        if expected_date is None or actual_date is None:
            return False, "One value is not a valid ISO-8601 or day-first dotted date."
        return expected_date == actual_date, "Compared calendar dates."
    try:
        expected_number = Decimal(expected)
        actual_number = Decimal(actual)
        if not expected_number.is_finite() or not actual_number.is_finite():
            return False, "One value is not finite numeric data."
        difference = abs(expected_number - actual_number)
        absolute_ok = absolute_tolerance is not None and difference <= Decimal(
            str(absolute_tolerance)
        )
        relative_ok = relative_tolerance is not None and (
            difference == 0
            if expected_number == 0
            else difference / abs(expected_number) <= Decimal(str(relative_tolerance))
        )
        return difference == 0 or absolute_ok or relative_ok, (
            f"Numeric difference {difference}; absolute tolerance={absolute_tolerance}; "
            f"relative tolerance={relative_tolerance}."
        )
    except DecimalException:
        return False, "One value is invalid or outside the supported numeric range."


def _comparison_id(field_id: str) -> str:
    return hashlib.sha256(f"{field_id}:comparison".encode()).hexdigest()


def _pointer_child(pointer: str, token: str) -> str:
    """A row's exact reference address: the object's pointer plus the row key
    as one RFC 6901 token (escaped, so a raw reference key containing "/" or
    "~" still names one key)."""
    return f"{pointer}/{token.replace('~', '~0').replace('/', '~1')}"


class ComparisonEngine:
    def compare(self, plan: ComparisonPlan, extraction: ExtractionResult) -> list[FieldComparison]:
        """Compare every planned field and return one FieldComparison each.

        A rows field returns one comparison per generated row, one per
        reference key no row matched, and one for a generator-level note.
        Fields that were not extracted, were ambiguous, or have no reference
        value are reported as not_compared or ambiguous, never as a match or
        mismatch. Raises ValueError when the plan and the extraction were
        built from different specifications.
        """
        if plan.specification_id != extraction.specification_id:
            raise ValueError("plan and extraction use different specifications")
        extracted_by_id = {field.field_id: field for field in extraction.fields}
        row_groups: dict[str, list[FieldExtraction]] = {}
        row_notes: dict[str, FieldExtraction] = {}
        for entry in extraction.fields:
            if entry.row_generator_id is None:
                continue
            if entry.row_key is None:
                row_notes[entry.row_generator_id] = entry
            else:
                row_groups.setdefault(entry.row_generator_id, []).append(entry)
        comparisons: list[FieldComparison] = []
        for field in plan.fields:
            if field.rows is not None:
                comparisons.extend(
                    self._compare_rows(
                        plan, field, row_groups.get(field.id, []), row_notes.get(field.id)
                    )
                )
                continue
            extracted = extracted_by_id.get(field.id)
            if extracted is None or extracted.status is ExtractionStatus.NOT_EXTRACTED:
                extraction_status = (
                    extracted.status if extracted is not None else ExtractionStatus.NOT_EXTRACTED
                )
                explanation = (
                    extracted.explanation
                    if extracted is not None
                    else "No extraction was produced."
                )
                status = ComparisonStatus.NOT_COMPARED
                matched = False
            elif extracted.status is ExtractionStatus.AMBIGUOUS:
                extraction_status = extracted.status
                explanation = extracted.explanation
                status = ComparisonStatus.AMBIGUOUS
                matched = False
            elif extracted.status is ExtractionStatus.ERROR:
                extraction_status = extracted.status
                explanation = extracted.explanation
                status = ComparisonStatus.NOT_COMPARED
                matched = False
            elif not field.expected_value_present:
                extraction_status = extracted.status
                explanation = "Optional reference value is absent; comparison was skipped."
                status = ComparisonStatus.NOT_COMPARED
                matched = False
            else:
                extraction_status = extracted.status
                expected = _normalize(field.expected_value, field.compare.normalizers)
                actual = _normalize(extracted.normalized_document_value, field.compare.normalizers)
                matched, explanation = _compare(
                    expected,
                    actual,
                    field.compare.operator,
                    field.compare.absolute_tolerance,
                    field.compare.relative_tolerance,
                )
                status = ComparisonStatus.MATCH if matched else ComparisonStatus.MISMATCH
            comparisons.append(
                FieldComparison(
                    id=_comparison_id(field.id),
                    field_id=field.id,
                    section_key=field.section_key,
                    section_label=field.section_label,
                    field_key=field.key,
                    field_label=field.label,
                    reference_pointer=field.reference_pointer,
                    reference_value=field.expected_value,
                    reference_value_present=field.expected_value_present,
                    extraction_status=extraction_status,
                    document_value=extracted.raw_document_value if extracted else None,
                    normalized_document_value=(
                        extracted.normalized_document_value if extracted else None
                    ),
                    document_location=extracted.location if extracted else None,
                    column_context=extracted.column_context if extracted else None,
                    row_context=extracted.row_context if extracted else None,
                    status=status,
                    confidence=extracted.confidence if extracted else 0,
                    severity=field.compare.severity
                    if status is ComparisonStatus.MISMATCH
                    else None,
                    explanation=explanation,
                    comparator=field.compare.operator,
                    normalizers=field.compare.normalizers,
                    absolute_tolerance=field.compare.absolute_tolerance,
                    relative_tolerance=field.compare.relative_tolerance,
                )
            )
        return comparisons

    def _compare_rows(
        self,
        plan: ComparisonPlan,
        field: PlannedField,
        row_entries: list[FieldExtraction],
        note: FieldExtraction | None,
    ) -> list[FieldComparison]:
        """Compare a rows field against the reference object its pointer
        addresses: one comparison per generated row bound to the object's key
        of the same name, one loud comparison per reference key no row
        matched, and one for the generator-level note (no label matched, or
        the max_rows cap fired). Both missing directions are mismatches, so a
        row missing on either side can never pass silently."""
        expected_object = (
            field.expected_value
            if field.expected_value_present and isinstance(field.expected_value, dict)
            else None
        )
        comparisons: list[FieldComparison] = []
        matched: set[str] = set()
        for entry in row_entries:
            if entry.row_key is None:
                continue
            matched.add(entry.row_key)
            comparisons.append(self._row_comparison(field, entry, entry.row_key, expected_object))
        if expected_object is not None:
            for key in expected_object:
                if key in matched:
                    continue
                row_id = field_identifier(plan.specification_id, field.section_key, field.key, key)
                comparisons.append(
                    FieldComparison(
                        id=_comparison_id(row_id),
                        field_id=row_id,
                        section_key=field.section_key,
                        section_label=field.section_label,
                        field_key=f"{field.key}.{key}",
                        field_label=f"{field.label} ({key})",
                        reference_pointer=_pointer_child(field.reference_pointer, key),
                        reference_value=expected_object[key],
                        reference_value_present=True,
                        extraction_status=ExtractionStatus.NOT_EXTRACTED,
                        document_value=None,
                        normalized_document_value=None,
                        document_location=None,
                        column_context=None,
                        row_context=None,
                        status=ComparisonStatus.MISMATCH,
                        confidence=0,
                        severity=field.compare.severity,
                        explanation=(
                            f"Reference key {key!r} at {field.reference_pointer!r} has no "
                            "matching document row (ROW_NOT_IN_DOCUMENT)."
                        ),
                        comparator=field.compare.operator,
                        normalizers=field.compare.normalizers,
                        absolute_tolerance=field.compare.absolute_tolerance,
                        relative_tolerance=field.compare.relative_tolerance,
                    )
                )
        if note is not None:
            comparisons.append(
                FieldComparison(
                    id=_comparison_id(field.id),
                    field_id=field.id,
                    section_key=field.section_key,
                    section_label=field.section_label,
                    field_key=field.key,
                    field_label=field.label,
                    reference_pointer=field.reference_pointer,
                    reference_value=field.expected_value,
                    reference_value_present=field.expected_value_present,
                    extraction_status=note.status,
                    document_value=note.raw_document_value,
                    normalized_document_value=note.normalized_document_value,
                    document_location=note.location,
                    column_context=note.column_context,
                    row_context=note.row_context,
                    status=ComparisonStatus.NOT_COMPARED,
                    confidence=note.confidence,
                    severity=None,
                    explanation=note.explanation,
                    comparator=field.compare.operator,
                    normalizers=field.compare.normalizers,
                    absolute_tolerance=field.compare.absolute_tolerance,
                    relative_tolerance=field.compare.relative_tolerance,
                )
            )
        return comparisons

    def _row_comparison(
        self,
        field: PlannedField,
        entry: FieldExtraction,
        row_key: str,
        expected_object: dict[str, Any] | None,
    ) -> FieldComparison:
        row_field_key = f"{field.key}.{row_key}"
        if entry.status is not ExtractionStatus.EXTRACTED:
            status = (
                ComparisonStatus.AMBIGUOUS
                if entry.status is ExtractionStatus.AMBIGUOUS
                else ComparisonStatus.NOT_COMPARED
            )
            explanation = entry.explanation
        elif expected_object is None:
            status = ComparisonStatus.NOT_COMPARED
            explanation = "Optional reference value is absent; comparison was skipped."
        elif row_key not in expected_object:
            status = ComparisonStatus.MISMATCH
            explanation = (
                f"Reference object at {field.reference_pointer!r} has no key {row_key!r} for "
                f"generated row {row_field_key!r} (ROW_NOT_IN_REFERENCE)."
            )
        else:
            expected_raw = expected_object[row_key]
            expected_text = _normalize(expected_raw, field.compare.normalizers)
            actual_text = _normalize(entry.normalized_document_value, field.compare.normalizers)
            matched, explanation = _compare(
                expected_text,
                actual_text,
                field.compare.operator,
                field.compare.absolute_tolerance,
                field.compare.relative_tolerance,
            )
            status = ComparisonStatus.MATCH if matched else ComparisonStatus.MISMATCH
        return FieldComparison(
            id=_comparison_id(entry.field_id),
            field_id=entry.field_id,
            section_key=field.section_key,
            section_label=field.section_label,
            field_key=row_field_key,
            field_label=f"{field.label} ({row_key})",
            reference_pointer=_pointer_child(field.reference_pointer, row_key),
            reference_value=(expected_object.get(row_key) if expected_object is not None else None),
            reference_value_present=field.expected_value_present,
            extraction_status=entry.status,
            document_value=entry.raw_document_value,
            normalized_document_value=entry.normalized_document_value,
            document_location=entry.location,
            column_context=entry.column_context,
            row_context=entry.row_context,
            status=status,
            confidence=entry.confidence,
            severity=field.compare.severity if status is ComparisonStatus.MISMATCH else None,
            explanation=explanation,
            comparator=field.compare.operator,
            normalizers=field.compare.normalizers,
            absolute_tolerance=field.compare.absolute_tolerance,
            relative_tolerance=field.compare.relative_tolerance,
        )

    @staticmethod
    def summarize(comparisons: list[FieldComparison]) -> ReviewSummary:
        """Count outcomes, rates, and severities across all comparisons."""
        matched = sum(item.status is ComparisonStatus.MATCH for item in comparisons)
        mismatched = sum(item.status is ComparisonStatus.MISMATCH for item in comparisons)
        not_compared = sum(item.status is ComparisonStatus.NOT_COMPARED for item in comparisons)
        ambiguous = sum(item.status is ComparisonStatus.AMBIGUOUS for item in comparisons)
        compared = matched + mismatched
        by_severity: dict[str, int] = {}
        for item in comparisons:
            if item.severity is not None:
                by_severity[item.severity.value] = by_severity.get(item.severity.value, 0) + 1
        return ReviewSummary(
            total_fields=len(comparisons),
            matched=matched,
            mismatched=mismatched,
            not_compared=not_compared,
            ambiguous=ambiguous,
            coverage_rate=compared / len(comparisons) if comparisons else 0,
            agreement_rate=matched / compared if compared else None,
            by_severity=by_severity,
        )
