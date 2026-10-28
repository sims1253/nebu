"""Text helpers shared by the source clients."""

import re
from html import unescape

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def strip_markup(text: str | None) -> str | None:
    """Turn the JATS/HTML fragments some sources return into plain abstract text."""
    if not text:
        return None
    plain = unescape(_TAG.sub(" ", text))
    return _SPACE.sub(" ", plain).strip() or None


def title_and_abstract(title: str, abstract: str | None) -> str:
    """The text entity offsets point into: title, newline, abstract."""
    return title if abstract is None else f"{title}\n{abstract}"
