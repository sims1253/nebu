"""Compare saved extraction data with independently supplied reference data."""

from datetime import date, datetime
from decimal import Decimal, DecimalException, localcontext
from typing import Any

from janus.comparison.reference import resolve_json_pointer
from janus.extraction.models import CheckResult, CheckRule, Scalar, StructuredExtraction
from janus.extraction.parsing import parse_value


def compare(
    extraction: StructuredExtraction,
    reference: Any,
    rules: list[CheckRule],
) -> list[CheckResult]:
    """Compare explicit output/reference pointers. Never reads or re-extracts a PDF."""
    if not rules:
        raise ValueError("At least one comparison rule is required.")
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
                        precision = (
                            max(a.adjusted(), b.adjusted())
                            - min(int(a.as_tuple().exponent), int(b.as_tuple().exponent))
                            + 2
                        )
                        if precision > 10_000:
                            raise ValueError("Numeric comparison exceeds 10,000 decimal places.")
                        with localcontext() as context:
                            context.prec = max(28, precision)
                            difference = abs(a - b)
                            match = difference <= Decimal(str(rule.absolute_tolerance))
                        message = f"Absolute difference: {difference}. Tolerance: {rule.absolute_tolerance}."
                    else:
                        normalized_expected = expected
                        if field.type == "date":
                            if isinstance(expected, datetime):
                                normalized_expected = expected.date().isoformat()
                            elif isinstance(expected, date):
                                normalized_expected = expected.isoformat()
                            elif isinstance(expected, str):
                                normalized_expected = parse_value(expected, Scalar(type="date"))
                            else:
                                raise ValueError("Expected a date in the reference data.")
                        elif field.type == "boolean" and isinstance(expected, str):
                            normalized_expected = parse_value(expected, Scalar(type="boolean"))
                        match = (
                            type(actual) is type(normalized_expected)
                            and actual == normalized_expected
                        )
                        message = "Compared typed values for equality."
                    status = "match" if match else "mismatch"
                except (DecimalException, ValueError) as exc:
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
