"""Canonical JSON (RFC 8785, JSON Canonicalization Scheme) for content hashes.

Object members are sorted by their UTF-16 code units, no whitespace is emitted, strings use
the shortest escapes, and numbers are written the way ECMAScript prints them. Equal JSON
content gives equal bytes regardless of member order or serializer.
"""

import json
import math
from decimal import Decimal
from typing import Any


def _number(value: float) -> str:
    """ECMAScript's Number::toString, which RFC 8785 adopts, from the shortest round-trip
    digits that Python's repr also uses."""
    if not math.isfinite(value):
        raise ValueError("Canonical JSON has no representation for NaN or infinity.")
    if value == 0:
        return "0"
    _sign, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(map(str, digit_tuple)).rstrip("0")
    exponent = int(exponent) + len(digit_tuple) - len(digits)
    k = len(digits)
    n = exponent + k  # value is 0.<digits> times 10**n
    prefix = "-" if value < 0 else ""
    if k <= n <= 21:
        return prefix + digits + "0" * (n - k)
    if 0 < n <= 21:
        return f"{prefix}{digits[:n]}.{digits[n:]}"
    if -6 < n <= 0:
        return f"{prefix}0.{'0' * -n}{digits}"
    power = f"e{'+' if n - 1 > 0 else '-'}{abs(n - 1)}"
    return prefix + (digits if k == 1 else f"{digits[0]}.{digits[1:]}") + power


def _encode(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be"))
        return "{" + ",".join(f"{_encode(key)}:{_encode(value[key])}" for key in keys) + "}"
    raise TypeError(f"{type(value).__name__} is not JSON data.")


def canonical_json(value: Any) -> bytes:
    """`value` (JSON data: dicts with string keys, lists, strings, numbers, booleans, None) as
    RFC 8785 canonical UTF-8 bytes."""
    return _encode(value).encode()
