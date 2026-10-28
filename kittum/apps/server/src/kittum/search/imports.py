"""File import adapters for RIS (generic, Cochrane CENTRAL, Embase) and CSV (CTIS, EU-CTR)."""

import csv
import io
import json
import re
from typing import TYPE_CHECKING, Any

import rispy

from kittum.core import Clock, Id, SystemClock, new_id, sha256_hex
from kittum.ledger import GENESIS_HASH, EventDraft, Ledger, event_hash, payload_hash
from kittum.search.models import SearchExecution, SearchPlan, SourceRecord
from kittum.search.store import save_execution

if TYPE_CHECKING:
    from kittum.db import Tx
    from kittum.storage import ArtifactStore

NCT_PATTERN = re.compile(r"(?i)\b(NCT\d{8})\b")
EUDRACT_PATTERN = re.compile(r"\b(\d{4}-\d{6}-\d{2}(?:-\d{2})?)\b")
YEAR_PATTERN = re.compile(r"\b(19\d\d|20\d\d)\b")


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
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def parse_ris(
    text: str, raw_hash: str, format_name: str, start_ordinal: int = 0
) -> list[SourceRecord]:
    """Parse RIS text into SourceRecord models."""
    entries = rispy.loads(text)
    records: list[SourceRecord] = []
    ordinal = start_ordinal

    for entry in entries:
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
        ordinal += 1

    return records


def _get_col(norm: dict[str, str], *keys: str) -> str | None:
    for k in keys:
        if norm.get(k):
            return norm[k].strip()
    return None


def parse_csv(
    text: str, raw_hash: str, format_name: str, start_ordinal: int = 0
) -> list[SourceRecord]:
    """Parse CTIS or EU-CTR CSV into SourceRecord models."""
    reader = csv.DictReader(io.StringIO(text))
    records: list[SourceRecord] = []
    ordinal = start_ordinal

    for row in reader:
        # Normalize keys
        norm = {k.strip().lower().replace(" ", "_"): v for k, v in row.items() if k}

        record_id = (
            _get_col(
                norm,
                "eu_ct_number",
                "eudract_number",
                "trial_number",
                "id",
                "protocol_number",
                "sponsor_protocol_number",
            )
            or f"{format_name}-{ordinal}"
        )

        title = _get_col(norm, "trial_title", "full_title", "title") or ""
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
        ordinal += 1

    return records


def import_file(
    tx: Tx,
    project_id: Id,
    plan: SearchPlan,
    data: bytes | str,
    format: str,
    *,
    ledger: Ledger | None = None,
    actor_id: Id | None = None,
    artifacts: ArtifactStore | None = None,
    clock: Clock | None = None,
) -> SearchExecution:
    """Import records from an exported file (RIS, CENTRAL, Embase, CTIS, EU-CTR)."""
    now = (clock or SystemClock()).now()
    raw_bytes = data.encode("utf-8") if isinstance(data, str) else data
    raw_hash = sha256_hex(raw_bytes)
    text_content = raw_bytes.decode("utf-8", errors="replace")

    if artifacts is not None:
        artifacts.put(raw_bytes)

    fmt = format.lower()
    if fmt in ("ris", "central", "centralexport", "embase"):
        records = parse_ris(text_content, raw_hash, plan.source)
    elif fmt in ("csv", "ctis", "euctr", "eu-ctr"):
        records = parse_csv(text_content, raw_hash, plan.source)
    else:
        # Default to RIS attempt, then CSV
        try:
            records = parse_ris(text_content, raw_hash, plan.source)
        except Exception:
            records = parse_csv(text_content, raw_hash, plan.source)

    count = len(records)
    exec_id = new_id()

    execution = SearchExecution(
        id=exec_id,
        plan=plan,
        kind="file_import",
        started_at=now,
        ended_at=now,
        query_translation=None,
        reported_count=count,
        retrieved_count=count,
        raw_artifact_hashes=(raw_hash,),
        records=tuple(records),
    )

    save_execution(tx, project_id, execution)

    if ledger is not None:
        draft = EventDraft(
            project_id=project_id,
            type="search.executed",
            schema_version=1,
            actor_id=actor_id or new_id(),
            payload={
                "execution_id": str(execution.id),
                "topic_id": plan.topic_id,
                "source": plan.source,
                "platform": plan.platform,
                "kind": execution.kind,
                "strategy": plan.strategy,
                "limits": list(plan.limits),
                "query_translation": execution.query_translation,
                "reported_count": execution.reported_count,
                "retrieved_count": execution.retrieved_count,
                "started_at": execution.started_at.isoformat(),
                "ended_at": execution.ended_at.isoformat(),
                "raw_artifact_hashes": list(execution.raw_artifact_hashes),
            },
        )
        ledger.append(tx, draft)
    elif type(tx).__name__ != "NullTx":
        _append_postgres_ledger(tx, project_id, actor_id or new_id(), execution, now)

    return execution


def _append_postgres_ledger(
    tx: Tx, project_id: Id, actor_id: Id, execution: SearchExecution, now: Any
) -> None:
    # Ensure system actor exists for FK constraint
    actor_row = tx.execute(
        """
        insert into actor (identity_ref, kind) values ('system', 'system')
        on conflict (identity_ref) do update set identity_ref = excluded.identity_ref
        returning id
        """
    ).fetchone()
    effective_actor_id = actor_row["id"] if actor_row else actor_id

    payload = {
        "execution_id": str(execution.id),
        "topic_id": execution.plan.topic_id,
        "source": execution.plan.source,
        "platform": execution.plan.platform,
        "kind": execution.kind,
        "strategy": execution.plan.strategy,
        "limits": list(execution.plan.limits),
        "query_translation": execution.query_translation,
        "reported_count": execution.reported_count,
        "retrieved_count": execution.retrieved_count,
        "started_at": execution.started_at.isoformat(),
        "ended_at": execution.ended_at.isoformat(),
        "raw_artifact_hashes": list(execution.raw_artifact_hashes),
    }

    last = tx.execute(
        """
        select seq, event_hash from audit_event
        where project_id = %s
        order by seq desc limit 1 for update
        """,
        (project_id,),
    ).fetchone()

    seq = (last["seq"] + 1) if last else 1
    prev_h = last["event_hash"] if last else GENESIS_HASH
    event_id = new_id()
    p_hash = payload_hash(payload)
    e_hash = event_hash(
        project_id=project_id,
        seq=seq,
        event_id=event_id,
        type="search.executed",
        schema_version=1,
        actor_id=effective_actor_id,
        recorded_at=now,
        payload_hash=p_hash,
        prev_hash=prev_h,
    )

    tx.execute(
        """
        insert into audit_event (
            project_id, seq, event_id, type, schema_version, actor_id,
            recorded_at, payload, payload_hash, prev_hash, event_hash
        ) values (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        """,
        (
            project_id,
            seq,
            event_id,
            "search.executed",
            1,
            effective_actor_id,
            now,
            json.dumps(payload),
            p_hash,
            prev_h,
            e_hash,
        ),
    )
