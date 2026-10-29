"""Whether a PDF is the report a record names, before it becomes the record's full text.

A source can name the wrong file: Unpaywall and Semantic Scholar both gave a JMIR trial's CONSORT
checklist, a supplement, as the trial's PDF. So a fetched or uploaded PDF is checked against the
record: the record's DOI on its first pages, or the record's title on its first two.

A DOI counts only as a whole identifier, never as the start of a longer one (10.1234/abc is not in
10.1234/abc-def, 10.1234/abc(20)12345, or 10.1234/abc wrapped onto a next line that starts with
"-def"). It may wrap across lines, and a hyphen the line break added is dropped, while a DOI's own
hyphens and parentheses still match. A title counts when a stretch of a page that covers the whole
title is at least `TITLE_THRESHOLD` similar to it, ignoring spacing, case, accents, punctuation,
and hyphenation; a page shorter than the title, or one that ends inside it, cannot show it.

The check prefers precision to recall: a running header printed inside a title can make it reject
the right PDF, which only sends Kittum on to the next source or the lead to an upload, while
accepting a wrong PDF stores another document as the report, the bug of issue #60. The checklist carried
the trial's draft title, 89 similar to the published one, on its second page. A PDF without text on
its first pages, or a record without a DOI or a usable title, cannot be checked; the caller
decides what that means.
"""

import functools
import re
import unicodedata
from typing import Literal

import pymupdf
from rapidfuzz import fuzz

from kittum.core import Record, canonical_doi

DOI_PAGES = 3
TITLE_PAGES = 2
TITLE_THRESHOLD = 95.0
MIN_TITLE = 20
"""Normalized characters a title needs before it can decide anything: a short title such as
"Editorial" is found in any page."""

type Verdict = Literal["match", "mismatch", "unchecked"]


class ReportCheck(Record):
    verdict: Verdict
    by: Literal["doi", "title"] | None = None
    """What showed the PDF is the record's report, on a match."""
    reason: str
    title_score: float | None = None


_GAP = r"(?:-?[ \t]*\n[ \t]*)?"
"""Where a DOI may wrap: a line break, with or without the hyphen the break added."""

_CONTINUES = "-(/_"
"""Marks that continue a DOI at the start of the next line. On the DOI's own line, every
character continues it except a space and the marks in `_ENDS` and `_continues`."""

_ENDS = "\"'\u201d\u2019\u00bb>"
"""Closing quotes and an angle bracket, which end a DOI quoted or linked in the text."""


def _continues(rest: str, doi: str) -> bool:
    """Whether the text after a found DOI continues it into a longer identifier."""
    if not rest:
        return False
    first = rest[0]
    if first in ".,;:":
        return len(rest) > 1 and rest[1].isalnum()  # "10.1234/abc.5" goes on; "abc." ends
    if first in ")]":
        # It closes a bracket outside the DOI, unless the DOI left one of its own open.
        opening = "(" if first == ")" else "["
        return doi.count(opening) > doi.count(first)
    if first in _ENDS:
        return False
    if first in " \t\n":
        line = rest.lstrip(" \t")
        if line.startswith("\n"):
            following = line[1:].lstrip(" \t")
            # Prose on the next line starts a new line of text; a hyphen or bracket goes on.
            return bool(following) and following[0] in _CONTINUES
        return False
    # Anything else on the same line, "+" and "=" included, may be part of a DOI suffix.
    return True


def _shows_doi(pages: tuple[str, ...], doi: str) -> bool:
    """`doi` as a whole identifier on one of the pages: not inside a longer DOI. It may wrap
    across lines (`_GAP`), and the text after it must not continue it (`_continues`). A DOI starts
    with "10.", so a letter, digit, or full stop before it would make it part of another."""
    body = _GAP.join(re.escape(char) for char in doi)
    pattern = re.compile(rf"(?<![0-9a-z.]){body}", re.IGNORECASE)
    return any(
        not _continues(page[found.end() :], doi)
        for page in pages
        for found in pattern.finditer(page)
    )


def _title_score(title: str, page: str) -> float:
    """How well the best stretch of `page` as long as the whole `title` matches it, 0 to 100. A
    stretch the page cuts short (a page shorter than the title, or one that ends inside it) does
    not count."""
    size = len(title)
    if len(page) < size:
        return 0.0
    best = fuzz.partial_ratio_alignment(title, page)
    if best is not None and best.dest_end - best.dest_start == size:
        return best.score
    return max(fuzz.ratio(title, page[i : i + size]) for i in range(len(page) - size + 1))


@functools.lru_cache(maxsize=32)
def _letters(text: str) -> str:
    """Lower-case letters and digits only, accents taken off. Cached: a bulk upload checks the same
    pages against every report without a full text, and normalizes each page once."""
    folded = unicodedata.normalize("NFKD", text).casefold()
    return "".join(re.findall(r"[a-z0-9]+", folded))


def _usable_title(title: str | None) -> str | None:
    """The title as compared, or `None` when it cannot decide: too short, or PubMed's bracketed
    English translation of a title the PDF prints in its own language."""
    if not title:
        return None
    stripped = title.strip().rstrip(".")
    if stripped.startswith("[") and stripped.endswith("]"):
        return None
    letters = _letters(stripped)
    return letters if len(letters) >= MIN_TITLE else None


def first_pages(pdf: bytes) -> tuple[str, ...] | None:
    """The text of the pages `check_pages` reads, or `None` when the PDF does not open."""
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            count = min(max(DOI_PAGES, TITLE_PAGES), document.page_count)
            return tuple(document[i].get_text() for i in range(count))
    except Exception:
        return None


def check_report(pdf: bytes, *, doi: str | None, title: str | None) -> ReportCheck:
    """Check `pdf` against the record's DOI and title."""
    return check_pages(first_pages(pdf), doi=doi, title=title)


def check_pages(
    pages: tuple[str, ...] | None, *, doi: str | None, title: str | None
) -> ReportCheck:
    """`check_report` on pages `first_pages` read, to check one PDF against many records."""
    if pages is None:
        return ReportCheck(verdict="unchecked", reason="The PDF does not open.")
    if not any(page.strip() for page in pages):
        return ReportCheck(verdict="unchecked", reason="The first pages have no text layer.")
    wanted_doi = canonical_doi(doi)
    wanted_title = _usable_title(title)
    if wanted_doi is None and wanted_title is None:
        return ReportCheck(verdict="unchecked", reason="The record has no DOI or usable title.")
    if wanted_doi is not None and _shows_doi(pages[:DOI_PAGES], wanted_doi):
        return ReportCheck(verdict="match", by="doi", reason=f"The PDF shows DOI {wanted_doi}.")
    score = None
    if wanted_title is not None:
        score = max(_title_score(wanted_title, _letters(p)) for p in pages[:TITLE_PAGES])
        if score >= TITLE_THRESHOLD:
            return ReportCheck(
                verdict="match",
                by="title",
                reason="The PDF shows the record's title.",
                title_score=round(score, 1),
            )
    return ReportCheck(
        verdict="mismatch",
        reason="The first pages show neither the record's DOI nor its title.",
        title_score=round(score, 1) if score is not None else None,
    )


__all__ = ["TITLE_THRESHOLD", "ReportCheck", "check_pages", "check_report", "first_pages"]
