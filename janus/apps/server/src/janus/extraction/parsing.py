"""Parse values using declared formats; decimals remain exact JSON strings."""

import re
from datetime import date
from decimal import Decimal

from janus.comparison.models import ValueType
from janus.extraction.models import DEFAULT_NOT_REPORTED, Scalar

_NUMERIC = frozenset({ValueType.INTEGER, ValueType.DECIMAL, ValueType.PERCENTAGE})
# A short estimator or count label printed in front of a number: "RR:1.124 (0.978, 1.290)",
# "N = 355", "MD=-0.4". Only a label followed by a number is removed.
_VALUE_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,5}\s*[:=]\s*(?=[-+]?\.?\d)")


class NotReported(ValueError):
    """The document prints no value: a not-reported mark, or a compound cell without the
    declared component. Extraction reports the field as missing rather than invalid."""


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def parse_value(raw: str, rule: Scalar) -> str | int | bool:
    # Reuse compound syntax recognition without adding comparison behavior.
    from janus.comparison.extractor import MINUS_SIGNS, match_component

    text = raw.strip()
    marks = DEFAULT_NOT_REPORTED if rule.not_reported is None else rule.not_reported
    if text and _fold(text) in {_fold(mark) for mark in marks}:
        raise NotReported(f"The document prints {text!r}, which marks the value as not reported.")
    numeric = rule.type in _NUMERIC or rule.source_shape is not None
    if numeric:
        text = text.translate(MINUS_SIGNS)
    prefix, suffix = rule.strip_prefix, rule.strip_suffix
    if rule.source_shape is not None:
        # Affixes around the whole cell ("RR:1.124 (0.978, 1.290)") go before the shape match;
        # otherwise they belong to the component, as before.
        if prefix and text.startswith(prefix):
            text, prefix = text[len(prefix) :].strip(), ""
        if suffix and text.endswith(suffix):
            text, suffix = text[: -len(suffix)].strip(), ""
    if numeric and not prefix:
        text = _VALUE_LABEL.sub("", text, count=1)
    if rule.source_shape is not None:
        component, error, absent = match_component(rule, text)
        if component is None:
            raise (NotReported if absent else ValueError)(error)
        text = component.strip()
    if prefix:
        if not text.startswith(prefix):
            raise ValueError(f"Expected prefix {prefix!r}.")
        text = text[len(prefix) :].strip()
    if suffix:
        if not text.endswith(suffix):
            raise ValueError(f"Expected suffix {suffix!r}.")
        text = text[: -len(suffix)].strip()
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
