"""Finding a model's quote in the source text.

This is the check behind every AI exclusion and every extracted value: a quote counts only if it
appears in the source. Matching tolerates whitespace differences, soft hyphens, and a word split
by a hyphen at a line break. Nothing else is forgiven.
"""

import unicodedata
from dataclasses import dataclass

_SOFT_HYPHEN = "­"


@dataclass(frozen=True, slots=True)
class Span:
    """Half-open character offsets into the original text."""

    start: int
    end: int


def _normalize(text: str, *, join_hyphenated: bool) -> tuple[str, list[int]]:
    """Return normalized text and, for each normalized character, its offset in `text`."""
    out: list[str] = []
    origin: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == _SOFT_HYPHEN:
            i += 1
            continue
        if ch == "-" and join_hyphenated:
            j = i + 1
            while j < n and text[j] in " \t\r":
                j += 1
            if j < n and text[j] == "\n" and out and out[-1].isalpha():
                k = j + 1
                while k < n and text[k].isspace():
                    k += 1
                if k < n and text[k].isalpha():
                    i = k
                    continue
        if ch.isspace():
            if out and out[-1] != " ":
                out.append(" ")
                origin.append(i)
            i += 1
            continue
        out.append(ch)
        origin.append(i)
        i += 1
    if out and out[-1] == " ":
        out.pop()
        origin.pop()
    return "".join(out), origin


def find_quote(quote: str, text: str) -> tuple[Span, ...]:
    """Return every occurrence of `quote` in `text`, as spans of the NFC form of `text`.

    Layers store NFC text, so the spans are offsets into the layer.

    An empty result means the quote is not in the source. More than one span means the quote
    exists but cannot anchor evidence on its own; the caller decides what that means.
    """
    quote = unicodedata.normalize("NFC", quote)
    text = unicodedata.normalize("NFC", text)
    needle, _ = _normalize(quote, join_hyphenated=False)
    if not needle:
        return ()
    for join in (False, True):
        hay, origin = _normalize(text, join_hyphenated=join)
        spans: list[Span] = []
        start = hay.find(needle)
        while start != -1:
            end = start + len(needle) - 1
            spans.append(Span(origin[start], origin[end] + 1))
            start = hay.find(needle, start + 1)
        if spans:
            return tuple(spans)
    return ()
