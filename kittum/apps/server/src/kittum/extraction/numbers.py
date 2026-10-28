"""Reading printed numbers exactly: minus signs, labels, and number tokens in a text.

Printed tables use typographic minus signs and thin spaces, and label estimates inline
("RR:1.124 (0.978, 1.290)", "N = 355"). These helpers normalize only what has one meaning and
never round: a value stays the digits the document printed.
"""

import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kittum.extraction.schema import Kind

# Only the minus signs: an en dash is a range ("0.98 to 1.29"), never a sign.
_MINUS = str.maketrans({"\u2212": "-", "\ufe63": "-", "\uff0d": "-"})
_SPACES = re.compile(r"[\s\u2009\u202f\u00a0]+")
# A short estimator or count label in front of a value: "RR:", "MD:", "N =", "aOR".
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]{0,5})\s*[:=]\s*")
# A number as printed, with its sign. "." is the only decimal separator Kittum reads, so "5,785"
# and "5 785" with a thin space are never read as one number.
NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w]|\.\d)")
_DECIMAL = re.compile(r"^-?\d+(?:\.\d+)?$")
_P_VALUE = re.compile(r"^[<>\u2264\u2265]?\s*(?:0?\.\d+|0|1(?:\.0+)?)$")
_TO_ASCII_COMPARATOR = str.maketrans({"\u2264": "<", "\u2265": ">"})


def ascii_minus(text: str) -> str:
    """Minus signs as "-", keeping every other character, so offsets stay valid."""
    return text.translate(_MINUS)


def normalize(text: str) -> str:
    """Typographic minus signs as "-", and runs of (thin) spaces as one space."""
    return _SPACES.sub(" ", text.translate(_MINUS)).strip()


def strip_label(text: str) -> tuple[str, str | None]:
    """The text without a leading short label, and the label ("RR", "N"), if there was one."""
    match = _LABEL.match(text)
    if match is None:
        return text, None
    return text[match.end() :], match.group(1)


def numbers(text: str) -> list[str]:
    """Every number token in `text`, normalized, in order."""
    return NUMBER.findall(normalize(text))


def well_formed(value: str | None, kind: Kind) -> str | None:
    """Why `value` is not a well-formed exact value of this kind, or None if it is."""
    if value is None:
        return None
    if value != value.strip() or value == "":
        return "an empty or padded string is not a value; use null for not reported"
    if kind == "text":
        return None
    if kind == "p_value":
        return None if _P_VALUE.match(value) else f"{value!r} is not a p-value"
    if not _DECIMAL.match(value):
        return f"{value!r} is not an exact decimal"
    if kind == "count" and ("." in value or value.startswith("-")):
        return f"{value!r} is not a count"
    if kind == "percentage" and not 0 <= Decimal(value) <= 100:
        return f"{value!r} is not a percentage"
    if kind == "ratio" and Decimal(value) <= 0:
        return f"{value!r} is not a ratio"
    return None


def decimal(value: str | None) -> Decimal | None:
    """The value as a `Decimal`, or None for a non-number (text, "<0.001", not reported)."""
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def same_value(a: str | None, b: str | None) -> bool:
    """Whether two values are equal: numerically for numbers ("0.50" is "0.5"), else as text."""
    if a is None or b is None:
        return a is b
    da, db = decimal(a), decimal(b)
    if da is not None and db is not None:
        return da == db
    return normalize(a).casefold().translate(_TO_ASCII_COMPARATOR) == normalize(
        b
    ).casefold().translate(_TO_ASCII_COMPARATOR)


def printed_in(value: str, kind: Kind, quote: str) -> str | None:
    """The value as `quote` prints it, if the quote contains it; None if it does not.

    Numbers match whole number tokens with an equal value, so "355" is in "355 per group" but not
    in "3550", and "1.12" is not in "1.124". A p-value keeps its comparator ("<0.001"). Text must
    occur verbatim, up to whitespace.
    """
    text = normalize(quote)
    if kind == "text":
        needle = normalize(value)
        return needle if needle and needle.casefold() in text.casefold() else None
    candidate = normalize(value).translate(_TO_ASCII_COMPARATOR).removesuffix("%").strip()
    comparator = ""
    if kind == "p_value" and candidate[:1] in "<>":
        comparator, candidate = candidate[0], candidate[1:].strip()
    target = decimal(candidate)
    if target is None:
        return None
    for match in NUMBER.finditer(text):
        token = match.group(0).lstrip("+")
        if decimal(token) != target:
            continue
        if kind == "p_value":
            # "0.001" is not printed in "P < 0.001": the comparator must match too.
            before = text[: match.start()].rstrip().translate(_TO_ASCII_COMPARATOR)
            printed = before[-1:] if before[-1:] in ("<", ">") else ""
            if printed != comparator:
                continue
            return comparator + token
        return token
    return None
