"""Compare saved extraction data with independently supplied reference data."""

from decimal import Decimal, InvalidOperation
from typing import Any

from janus.comparison.reference import resolve_json_pointer
from janus.extraction.models import CheckResult, CheckRule, StructuredExtraction


def compare(
    extraction: StructuredExtraction,
    reference: Any,
    rules: list[CheckRule],
) -> list[CheckResult]:
    """Compare explicit output/reference pointers. Never reads or re-extracts a PDF."""
    fields = {field.path: field for field in extraction.fields}
    results = []
    for rule in rules:
        field = fields.get(rule.path)
        actual_present, actual = resolve_json_pointer(extraction.data, rule.path)
        expected_present, expected = resolve_json_pointer(reference, rule.reference_pointer)
        status = "not_compared"
        message = "The output field is missing, invalid, or ambiguous."
        if actual_present and field is not None and field.status == "extracted":
            if not expected_present:
                message = "The reference pointer does not exist."
            else:
                try:
                    if field.type in {"integer", "decimal", "percentage"}:
                        if isinstance(expected, bool):
                            raise ValueError("Expected numeric reference data.")
                        a, b = Decimal(str(actual)), Decimal(str(expected))
                        if not a.is_finite() or not b.is_finite():
                            raise ValueError("Expected finite numeric reference data.")
                        match = abs(a - b) <= Decimal(str(rule.absolute_tolerance))
                        message = f"Absolute difference: {abs(a - b)}. Tolerance: {rule.absolute_tolerance}."
                    else:
                        match = type(actual) is type(expected) and actual == expected
                        message = "Compared typed values for equality."
                    status = "match" if match else "mismatch"
                except (InvalidOperation, ValueError) as exc:
                    message = str(exc) or "The reference value is not a valid number."
        results.append(
            CheckResult(
                path=rule.path,
                reference_pointer=rule.reference_pointer,
                status=status,
                actual=actual,
                expected=expected,
                message=message,
            )
        )
    return results
