"""Whether a PDF is the report a record names, before it becomes the record's full text.

A source can name the wrong file: Unpaywall and Semantic Scholar both gave a JMIR trial's CONSORT
checklist, a supplement, as the trial's PDF. So a fetched or uploaded PDF is checked against the
record: the record's DOI on its first pages, or the record's title on its first two. Spacing,
case, accents, punctuation, and hyphenation at line ends do not count. A title matches when a
stretch of the pages is at least `TITLE_THRESHOLD` similar to it; the checklist carried the
trial's draft title, 89 similar to the published one, on its second page. A PDF without text on
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
    if wanted_doi is not None:
        squeezed = "".join("".join(pages[:DOI_PAGES]).split()).casefold()
        if wanted_doi.casefold() in squeezed:
            return ReportCheck(verdict="match", by="doi", reason=f"The PDF shows DOI {wanted_doi}.")
    score = None
    if wanted_title is not None:
        score = max(fuzz.partial_ratio(wanted_title, _letters(p)) for p in pages[:TITLE_PAGES])
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
