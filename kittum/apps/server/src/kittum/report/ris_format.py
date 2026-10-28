"""RIS exports: every search undeduplicated, the deduplicated corpus, and each scope's lists.

Each entry carries the provider's record ID as `AN`, registry IDs and decisions as `N1` notes,
and Kittum's identities as custom tags (`C1` occurrence, `C2` report, `C3` study), so a reference
manager keeps them and a file traces back to the bundle. The search module's RIS import reads
these files back. RIS has no line breaks inside a field, so each field is written on one line
(a structured abstract's sections run on); the bundle's database keeps the exact text.
"""

import re
from typing import TYPE_CHECKING, Any

import rispy

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kittum.core import Id
    from kittum.search import SourceRecord


_BREAK = re.compile(r"[ \t]*[\r\n]+[ \t]*")


def one_line(text: str) -> str:
    """A field value without line breaks, as RIS can carry it."""
    return _BREAK.sub(" ", text).strip()


class _Writer(rispy.RisWriter):
    def set_header(self, count: int) -> str:  # noqa: ARG002 - no "1." line before each entry
        return ""


def ris_entry(
    record: SourceRecord,
    *,
    registry: bool = False,
    report_id: Id | None = None,
    study_id: Id | None = None,
    notes: Iterable[str] = (),
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type_of_reference": "DBASE" if registry else "JOUR",
        "title": one_line(record.title),
        "accession_number": record.provider_record_id,
        "custom1": f"kittum:occurrence:{record.occurrence_id}",
    }
    if record.authors:
        out["authors"] = list(record.authors)
    if record.year is not None:
        out["year"] = str(record.year)
    if record.journal:
        out["journal_name"] = record.journal
    if record.doi:
        out["doi"] = record.doi
    if record.abstract:
        out["abstract"] = one_line(record.abstract)
    if record.pmid:
        out["urls"] = [f"https://pubmed.ncbi.nlm.nih.gov/{record.pmid}/"]
    if report_id is not None:
        out["custom2"] = f"kittum:report:{report_id}"
    if study_id is not None:
        out["custom3"] = f"kittum:study:{study_id}"
    note_lines = [*record.registry_ids, *notes]
    if record.pmid:
        note_lines.append(f"PMID: {record.pmid}")
    if note_lines:
        out["notes"] = note_lines
    return out


def ris_dumps(entries: Sequence[dict[str, Any]]) -> bytes:
    return rispy.dumps(list(entries), implementation=_Writer).encode()
