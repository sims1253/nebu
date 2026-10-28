"""The accepted extraction values as data files: one row per study and field.

`load_extracted` reads, as of a cutoff, the latest acceptance of every semantic key with the
study and report it belongs to, the path that produced the value, its source anchor (pages and
quote), and who accepted it when. The `extraction` target writes them as CSV and JSON, so a
review's results leave Kittum ready for analysis, not only as the papers they came from.

`check_bundle` is the offline check `kittum verify` runs on a bundle's copy: every value in
`reports/extraction/extracted_values.json` must match the `extraction.value_accepted` event the
ledger file holds for it, and every value the ledger accepted must be there. The hashes and the
signature already show the file is the one sealed; this shows the sealed file says what the
ledger says.
"""

import csv
import io
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from kittum.core import Id, Record

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from kittum.db import Tx
    from kittum.ledger import AuditEvent

VALUE_ACCEPTED = "extraction.value_accepted"
VALUES_PATH = "reports/extraction/extracted_values.json"
CSV_PATH = "reports/extraction/extracted_values.csv"

type ProducedBy = Literal["janus", "llm", "manual_edit"]


class ExtractedValue(Record):
    """One accepted value: the latest acceptance of its semantic key at the cutoff."""

    study_id: Id
    study: str
    report_id: Id
    report: str
    field: str
    """What was extracted, in words: "Hazard ratio, overall survival, 12 months, ivosidenib"."""
    quantity: str
    endpoint: str
    timepoint: str
    arm: str
    subpopulation: str
    analysis_set: str
    data_cut: str
    value: str | None
    unit: str | None
    produced_by: ProducedBy
    """The path whose value was accepted, or `manual_edit` when the reviewer edited it."""
    proposal_path: Literal["janus", "llm"]
    """The path whose proposal was accepted or edited."""
    pages: tuple[int, ...]
    quote: str | None
    anchor_id: Id
    accepted_by: str
    accepted_by_id: Id
    accepted_at: datetime
    acceptance_id: Id
    event_seq: int
    policy: str


def load_extracted(tx: Tx, project_id: Id, cutoff_seq: int) -> tuple[ExtractedValue, ...]:
    rows = tx.execute(
        "select distinct on (a.key_hash) a.id, a.path, a.edited, a.value, a.unit, a.anchor_id,"
        " a.reviewer_id, a.accepted_at, a.event_seq, f.key, f.spec, f.labels, r.report,"
        " n.quote, n.boxes,"
        " coalesce((select m.display_name from project_member m where m.actor_id = a.reviewer_id"
        "  and m.project_id = a.project_id limit 1), x.identity_ref) as reviewer"
        " from extraction_acceptance a"
        " join extraction_field f on f.id = a.field_id"
        " join extraction_run r on r.id = a.run_id"
        " left join anchor n on n.id = a.anchor_id"
        " left join actor x on x.id = a.reviewer_id"
        " where a.project_id = %s and a.event_seq <= %s"
        " order by a.key_hash, a.event_seq desc",
        (project_id, cutoff_seq),
    ).fetchall()
    found = []
    for r in rows:
        key, spec, labels, report = r["key"], r["spec"], r["labels"] or {}, r["report"] or {}
        quote = r["quote"]
        boxes = r["boxes"] or []
        endpoint = labels.get("endpoint") or key["endpoint"]
        timepoint = labels.get("timepoint") or key["timepoint"]
        arm = labels.get("arm") or key["arm"]
        found.append(
            ExtractedValue(
                study_id=key["study_id"],
                study=report.get("study_label") or str(key["study_id"]),
                report_id=key["report_id"],
                report=report.get("title") or report.get("citation") or str(key["report_id"]),
                field=", ".join(
                    part
                    for part in (
                        spec.get("description") or key["quantity"],
                        endpoint,
                        timepoint,
                        arm,
                    )
                    if part
                ),
                quantity=key["quantity"],
                endpoint=endpoint,
                timepoint=timepoint,
                arm=arm,
                subpopulation=key["subpopulation"],
                analysis_set=key["analysis_set"],
                data_cut=key["data_cut"],
                value=r["value"],
                unit=r["unit"],
                produced_by="manual_edit" if r["edited"] else r["path"],
                proposal_path=r["path"],
                pages=tuple(
                    sorted({b["page"] for b in boxes if isinstance(b, dict) and "page" in b})
                ),
                quote=quote.get("exact") if isinstance(quote, dict) else quote,
                anchor_id=r["anchor_id"],
                accepted_by=r["reviewer"] or str(r["reviewer_id"]),
                accepted_by_id=r["reviewer_id"],
                accepted_at=r["accepted_at"],
                acceptance_id=r["id"],
                event_seq=r["event_seq"],
                policy=spec.get("policy", ""),
            )
        )
    return tuple(sorted(found, key=lambda v: (v.study, v.report, v.endpoint, v.field)))


CSV_COLUMNS = (
    "study",
    "report",
    "field",
    "value",
    "unit",
    "quantity",
    "endpoint",
    "timepoint",
    "arm",
    "subpopulation",
    "analysis_set",
    "data_cut",
    "produced_by",
    "pages",
    "quote",
    "accepted_by",
    "accepted_at",
    "study_id",
    "report_id",
    "anchor_id",
    "acceptance_id",
    "event_seq",
)


def as_csv(values: tuple[ExtractedValue, ...]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for v in values:
        row = v.model_dump(mode="json")
        row["pages"] = " ".join(str(p) for p in v.pages)
        writer.writerow(["" if row[c] is None else row[c] for c in CSV_COLUMNS])
    return out.getvalue().encode()


def as_json(values: tuple[ExtractedValue, ...], cutoff_seq: int) -> dict[str, Any]:
    return {
        "cutoff_seq": cutoff_seq,
        "values": [v.model_dump(mode="json") for v in values],
    }


# The offline check of a bundle's copy.


def check_bundle(
    root: Path, events: list[AuditEvent], files: Mapping[str, str], problems: list[str]
) -> None:
    """Compare the bundle's extracted values with the ledger's acceptances (see the module)."""
    if VALUES_PATH not in files:
        return
    try:
        data = json.loads((root / VALUES_PATH).read_text())
        values = {v["acceptance_id"]: v for v in data["values"]}
        cutoff = int(data["cutoff_seq"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(f"{VALUES_PATH} cannot be read: {e}")
        return
    latest: dict[str, AuditEvent] = {}
    for e in events:
        if e.type == VALUE_ACCEPTED and e.seq <= cutoff:
            latest[str(e.payload.get("key_hash"))] = e
    by_seq = {e.seq: e for e in latest.values()}
    seen: set[int] = set()
    for v in values.values():
        e = by_seq.get(v.get("event_seq"))
        where = f"{VALUES_PATH}: {v.get('field')} of {v.get('study')}"
        if e is None:
            problems.append(f"{where} names event {v.get('event_seq')}, which accepted no value")
            continue
        seen.add(e.seq)
        p = e.payload
        produced = "manual_edit" if p.get("edited") else p.get("path")
        for name, ours, theirs in (
            ("value", v.get("value"), p.get("value")),
            ("unit", v.get("unit"), p.get("unit")),
            ("path", v.get("produced_by"), produced),
            ("anchor", v.get("anchor_id"), p.get("anchor_id")),
            ("reviewer", v.get("accepted_by_id"), str(e.actor_id)),
        ):
            if ours != theirs:
                problems.append(f"{where} has {name} {ours!r}, ledger event {e.seq} has {theirs!r}")
    for seq in sorted(set(by_seq) - seen):
        p = by_seq[seq].payload
        problems.append(
            f"{VALUES_PATH} leaves out the value {p.get('value')!r} accepted at ledger event {seq}"
        )
    if CSV_PATH in files:
        try:
            rows = list(csv.DictReader(io.StringIO((root / CSV_PATH).read_text())))
        except (OSError, ValueError, csv.Error) as e:
            problems.append(f"{CSV_PATH} cannot be read: {e}")
            return
        in_csv = {r.get("acceptance_id"): r for r in rows}
        if set(in_csv) != set(values):
            problems.append(f"{CSV_PATH} and {VALUES_PATH} list different values")
        for key, row in in_csv.items():
            v = values.get(key)
            if v is not None and (row.get("value"), row.get("unit")) != (
                v.get("value") or "",
                v.get("unit") or "",
            ):
                problems.append(f"{CSV_PATH}: {row.get('field')} differs from {VALUES_PATH}")
