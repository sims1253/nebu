"""What a source wrote about a failed request, safe to keep in the ledger and show to a person."""

import json
import re

MESSAGE_KEYS = ("errMsg", "message", "error", "detail", "msg")
MESSAGE_LENGTH = 300

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


def _clean(text: str) -> str | None:
    """One line, without control characters, at most `MESSAGE_LENGTH` characters."""
    text = _SPACE.sub(" ", _CONTROL.sub(" ", text)).strip()
    if not text:
        return None
    return text if len(text) <= MESSAGE_LENGTH else text[: MESSAGE_LENGTH - 1] + "…"


def source_message(body: bytes | None) -> str | None:
    """A JSON error field (Europe PMC's `errMsg`, a `message`, ...), or a short plain-text body.
    An HTML page (a Cloudflare challenge, a publisher's error page) says nothing worth keeping."""
    if not body:
        return None
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in MESSAGE_KEYS:
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if isinstance(value, str) and (found := _clean(value)):
                return found
        return None
    if data is None and text and not text.startswith("<") and len(text) <= MESSAGE_LENGTH:
        return _clean(text)
    return None


__all__ = ["MESSAGE_LENGTH", "source_message"]
