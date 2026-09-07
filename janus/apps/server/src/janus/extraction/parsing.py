"""Parse values using declared formats; decimals remain exact JSON strings."""

import re
from datetime import date
from decimal import Decimal

from janus.comparison.models import ValueType
from janus.extraction.models import Scalar


def parse_value(raw: str, rule: Scalar) -> str | int | bool:
    # Reuse compound syntax recognition without adding comparison behavior.
    from janus.comparison.extractor import extract_component

    if rule.source_shape is not None:
        component, error = extract_component(rule, raw)
        if error or component is None:
            raise ValueError(error)
        raw = component
    text = raw.strip()
    if rule.strip_prefix:
        if not text.startswith(rule.strip_prefix):
            raise ValueError(f"Expected prefix {rule.strip_prefix!r}.")
        text = text[len(rule.strip_prefix) :].strip()
    if rule.strip_suffix:
        if not text.endswith(rule.strip_suffix):
            raise ValueError(f"Expected suffix {rule.strip_suffix!r}.")
        text = text[: -len(rule.strip_suffix)].strip()
    if rule.type is ValueType.TEXT:
        return text
    if rule.type is ValueType.BOOLEAN:
        values = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}
        if text.casefold() not in values:
            raise ValueError("Expected true/false, yes/no, or 1/0.")
        return values[text.casefold()]
    if rule.type is ValueType.DATE:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise ValueError("Expected an ISO date (YYYY-MM-DD).") from exc
    if rule.type is ValueType.PERCENTAGE:
        text = text.removesuffix("%").strip()
    decimal = re.escape(rule.decimal_separator)
    integer = r"\d+"
    if rule.group_separator is not None:
        group = re.escape(rule.group_separator)
        integer = rf"(?:\d+|\d{{1,3}}(?:{group}\d{{3}})+)"
    if not re.fullmatch(rf"[+-]?{integer}(?:{decimal}\d+)?", text):
        raise ValueError("Number does not match the declared decimal and group separators.")
    if rule.group_separator:
        text = text.replace(rule.group_separator, "")
    number = Decimal(text.replace(rule.decimal_separator, "."))
    if rule.type is ValueType.INTEGER:
        if number != number.to_integral_value():
            raise ValueError("Expected a whole number.")
        # JSON consumers cannot represent arbitrary integers exactly.
        if abs(number) > 9_007_199_254_740_991:
            raise ValueError("Integer exceeds the exact JSON range; use decimal instead.")
        return int(number)
    return format(number, "f")
