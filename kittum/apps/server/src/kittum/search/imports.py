"""File import adapters for RIS (generic, Cochrane CENTRAL, Embase), CSV (CTIS, EU-CTR), and PubMed
XML (a PubMed or MEDLINE export saved as XML, as EFetch returns it)."""

import csv
import io
import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import rispy

from kittum.core import Clock, Id, SystemClock, new_id
from kittum.search.models import SearchExecution, SearchPlan, SourceRecord
from kittum.search.pubmed import parse_pubmed_xml
from kittum.search.service import record_execution

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.db import Tx
    from kittum.ledger import Ledger
    from kittum.storage import ArtifactStore

NCT_PATTERN = re.compile(r"(?i)\b(NCT\d{8})\b")
EUDRACT_PATTERN = re.compile(r"\b(\d{4}-\d{6}-\d{2}(?:-\d{2})?)\b")
YEAR_PATTERN = re.compile(r"\b(19\d\d|20\d\d)\b")
RIS_START = re.compile(r"(?m)^TY  - ")


def _find_registry_ids(*texts: str | None) -> tuple[str, ...]:
    combined = " ".join(t for t in texts if t)
    reg_ids: list[str] = []
    for nct in NCT_PATTERN.findall(combined):
        reg_ids.append(nct.upper())
    for eudra in EUDRACT_PATTERN.findall(combined):
        reg_ids.append(eudra)
    return tuple(dict.fromkeys(reg_ids))


def _parse_year(text: str | None) -> int | None:
    if not text:
        return None
    m = YEAR_PATTERN.search(str(text))
    return int(m.group(1)) if m else None


def parse_ris(text: str, raw_hash: str, format_name: str) -> list[SourceRecord]:
    """Parse RIS text into SourceRecord models."""
    entries = rispy.loads(text)
    # rispy skips anything it cannot read, including a record without `ER`; count `TY` lines.
    expected = len(RIS_START.findall(text))
    if len(entries) != expected or (expected == 0 and text.strip()):
        raise ValueError(f"RIS file has {expected} TY lines but {len(entries)} readable records")
    records: list[SourceRecord] = []

    for ordinal, entry in enumerate(entries):
        title = entry.get("title") or entry.get("primary_title") or ""
        abstract = entry.get("abstract") or entry.get("notes_abstract")

        authors_list = entry.get("authors") or entry.get("first_authors") or []
        if isinstance(authors_list, str):
            authors_list = [authors_list]
        authors = tuple(authors_list)

        year = _parse_year(entry.get("year") or entry.get("publication_year") or entry.get("date"))

        journal = (
            entry.get("journal_name")
            or entry.get("secondary_title")
            or entry.get("name_of_database")
        )

        doi = entry.get("doi")
        record_id = (
            entry.get("accession_number") or entry.get("id") or doi or f"{format_name}-{ordinal}"
        )

        type_ref = entry.get("type_of_reference")
        pub_types = (type_ref,) if type_ref else ("Journal Article",)

        notes = str(entry.get("notes") or "")
        reg_ids = _find_registry_ids(title, abstract, notes, record_id)

        rec = SourceRecord(
            occurrence_id=new_id(),
            source=format_name,
            provider_record_id=str(record_id),
            ordinal=ordinal,
            title=title,
            abstract=abstract,
            authors=authors,
            year=year,
            journal=journal,
            doi=doi,
            pmid=None,
            registry_ids=reg_ids,
            publication_types=pub_types,
            raw_hash=raw_hash,
        )
        records.append(rec)

    return records


CSV_ID_COLUMNS = (
    "eu_ct_number",
    "eudract_number",
    "trial_number",
    "id",
    "protocol_number",
    "sponsor_protocol_number",
)
CSV_TITLE_COLUMNS = ("trial_title", "full_title", "title")


def _norm_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_")


def _get_col(norm: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        if norm.get(k):
            return norm[k].strip()
    return None


def parse_csv(text: str, raw_hash: str, format_name: str) -> list[SourceRecord]:
    """Parse CTIS or EU-CTR CSV into SourceRecord models."""
    try:
        # `strict` turns an unterminated quoted field into an error instead of swallowing the rest.
        fieldnames, *rows = [row for row in csv.reader(io.StringIO(text), strict=True) if row] or [
            []
        ]
    except csv.Error as exc:
        raise ValueError(f"CSV file is malformed: {exc}") from exc
    keys = [_norm_key(k) for k in fieldnames]
    if not set(keys) & set(CSV_ID_COLUMNS) or not set(keys) & set(CSV_TITLE_COLUMNS):
        raise ValueError(
            f"CSV header needs a trial ID column ({', '.join(CSV_ID_COLUMNS)}) "
            f"and a title column ({', '.join(CSV_TITLE_COLUMNS)})"
        )
    for number, row in enumerate(rows, start=1):
        if len(row) != len(keys):
            raise ValueError(
                f"CSV record {number} has {len(row)} fields; the header has {len(keys)}"
            )
    records: list[SourceRecord] = []

    for ordinal, row in enumerate(rows):
        norm = {k: v for k, v in zip(keys, row, strict=True) if k}

        record_id = _get_col(norm, *CSV_ID_COLUMNS) or f"{format_name}-{ordinal}"

        title = _get_col(norm, *CSV_TITLE_COLUMNS) or ""
        abstract = _get_col(
            norm, "brief_summary", "medical_condition", "disease", "condition", "summary"
        )
        sponsor = _get_col(norm, "sponsor_name", "sponsor")
        status = _get_col(norm, "trial_status", "status")
        phase = _get_col(norm, "phase", "trial_phase")
        start_date = _get_col(norm, "start_date", "date")
        year = _parse_year(start_date)

        authors = (sponsor,) if sponsor else ()
        journal = sponsor

        pub_types: list[str] = []
        if phase:
            pub_types.append(f"Phase: {phase}" if not phase.lower().startswith("phase") else phase)
        if status:
            pub_types.append(f"Status: {status}")
        if not pub_types:
            pub_types.append("Clinical Trial")

        reg_ids = _find_registry_ids(record_id, title, abstract)
        if (
            record_id
            and (NCT_PATTERN.match(record_id) or EUDRACT_PATTERN.match(record_id))
            and record_id not in reg_ids
        ):
            reg_ids = (record_id, *reg_ids)

        rec = SourceRecord(
            occurrence_id=new_id(),
            source=format_name,
            provider_record_id=record_id,
            ordinal=ordinal,
            title=title,
            abstract=abstract,
            authors=authors,
            year=year,
            journal=journal,
            doi=None,
            pmid=None,
            registry_ids=reg_ids,
            publication_types=tuple(pub_types),
            raw_hash=raw_hash,
        )
        records.append(rec)

    return records


def parse_pubmed_export(text: str, raw_hash: str, format_name: str) -> list[SourceRecord]:
    """PubMed XML saved from PubMed (Send to, File, XML) or EFetch, under the plan's source."""
    try:
        records = parse_pubmed_xml(text.encode("utf-8"), raw_hash)
    except ET.ParseError as exc:
        raise ValueError(f"not PubMed XML: {exc}") from exc
    return [r.model_copy(update={"source": format_name}) for r in records]


_PARSERS: dict[str, Callable[[str, str, str], list[SourceRecord]]] = {
    "ris": parse_ris,
    "central": parse_ris,
    "centralexport": parse_ris,
    "embase": parse_ris,
    "csv": parse_csv,
    "ctis": parse_csv,
    "euctr": parse_csv,
    "eu-ctr": parse_csv,
    "pubmed-xml": parse_pubmed_export,
}


def import_file(
    tx: Tx,
    project_id: Id,
    plan: SearchPlan,
    data: bytes | str,
    format: str,
    *,
    ledger: Ledger,
    actor_id: Id,
    artifacts: ArtifactStore,
    clock: Clock | None = None,
) -> SearchExecution:
    """Import records from an exported file (RIS, CENTRAL, Embase, CTIS, EU-CTR, PubMed XML).

    The file is stored as the execution's one raw artifact. Its record count is the reported count;
    an export carries no separate platform total.
    """
    parse = _PARSERS.get(format.lower())
    if parse is None:
        raise ValueError(
            f"unsupported import format {format!r}; supported: {', '.join(sorted(_PARSERS))}"
        )
    raw_bytes = data.encode("utf-8") if isinstance(data, str) else data
    try:
        text_content = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"import file is not valid UTF-8: {exc}") from exc

    now = (clock or SystemClock()).now()
    raw_hash = artifacts.put(raw_bytes)
    records = parse(text_content, raw_hash, plan.source)

    execution = SearchExecution(
        id=new_id(),
        plan=plan,
        kind="file_import",
        started_at=now,
        ended_at=now,
        query_translation=None,
        reported_count=len(records),
        retrieved_count=len(records),
        raw_artifact_hashes=(raw_hash,),
        records=tuple(records),
    )
    return record_execution(
        tx, project_id, execution, ledger=ledger, actor_id=actor_id, artifacts=artifacts
    )
