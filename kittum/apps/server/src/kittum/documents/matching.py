"""Whether a PDF is the report a record names, before it becomes the record's full text.

A source can name the wrong file: Unpaywall and Semantic Scholar both gave a JMIR trial's CONSORT
checklist, a supplement, as the trial's PDF. So a fetched or uploaded PDF is checked against the
record: the record's DOI on its first pages, or the record's title on its first two.

A DOI counts only as a whole identifier, never as the start of a longer one (10.1234/wrong is not
in 10.1234/wrong-paper). It may wrap across lines, and a hyphen the line break added is dropped,
while a DOI's own hyphens still match. A title counts when a stretch of a page as long as the
whole title is at least `TITLE_THRESHOLD` similar to it, ignoring spacing, case, accents,
punctuation, and hyphenation; a page shorter than the title cannot show it. The checklist carried
the trial's draft title, 89 similar to the published one, on its second page. A PDF without text on
its first pages, or a record without a DOI or a usable title, cannot be checked; the caller
decides what that means.
"""

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


_DOI_CHAR = r"[^\s\"'<>()\[\]{},;]"
"""A character that can continue a DOI as printed."""


def _readings(page: str) -> tuple[str, ...]:
    """A page as printed, with wrapped lines joined, and with line-end hyphens dropped too."""
    joined = re.sub(r"[ \t]*\n[ \t]*", "", page)
    unhyphenated = re.sub(r"-[ \t]*\n[ \t]*", "", page)
    return (page, joined, re.sub(r"[ \t]*\n[ \t]*", "", unhyphenated))


def _shows_doi(pages: tuple[str, ...], doi: str) -> bool:
    """`doi` as a whole identifier on one of the pages: not inside a longer DOI. A DOI starts with
    "10.", so a letter, digit, or full stop before it would make it part of another; after it, a
    full stop or a closing bracket ends a sentence or a link, not the DOI."""
    pattern = re.compile(
        rf"(?<![0-9a-z.]){re.escape(doi)}(?!{_DOI_CHAR}*[^\s.\"'<>()\[\]{{}},;])",
        re.IGNORECASE,
    )
    return any(pattern.search(reading) for page in pages for reading in _readings(page))


def _title_score(title: str, page: str) -> float:
    """How well the best stretch of `page` as long as `title` matches it, 0 to 100."""
    if len(page) < len(title):
        return fuzz.ratio(title, page)
    return fuzz.partial_ratio(title, page)


def _letters(text: str) -> str:
    """Lower-case letters and digits only, accents taken off."""
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
