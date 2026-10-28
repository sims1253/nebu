"""The accepted extraction values as data files: one row per study and field.

`load_extracted` reads, as of a cutoff, the latest acceptance of every semantic key with the
study and report it belongs to, the path that produced the value, its source anchor (pages and
quote), and who accepted it when. The `extraction` target writes them as CSV and JSON, so a
review's results leave Kittum ready for analysis, not only as the papers they came from.

`check_bundle` is the offline check `kittum verify` runs on a bundle's copy: the file must show
the ledger at the bundle's own cutoff; every value in `reports/extraction/extracted_values.json`
must match, column by column, the `extraction.value_accepted` event the ledger file holds for it
and the field, run, anchor, and acceptance rows of the bundle; every value the ledger accepted
must be there; and the CSV must say what the JSON says. The hashes and the signature already show
the files are the ones sealed; this shows they say what the ledger says.
"""

import csv
import io
import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from kittum.core import Id, Record

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from kittum.db import Tx
    from kittum.ledger import AuditEvent

VALUE_ACCEPTED = "extraction.value_accepted"
MEMBER_ADDED = "project.member_added"
SQLITE_NAME = "kittum.sqlite"
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


def identity(
    key: Mapping[str, Any],
    spec: Mapping[str, Any],
    labels: Mapping[str, Any] | None,
    report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """What a value is about, in IDs and in words, from its semantic key, its field's spec and
    labels, and its run's report: the columns `check_bundle` rebuilds from the bundle."""
    labels, report = labels or {}, report or {}
    endpoint = labels.get("endpoint") or key["endpoint"]
    timepoint = labels.get("timepoint") or key["timepoint"]
    arm = labels.get("arm") or key["arm"]
    return {
        "study_id": str(key["study_id"]),
        "study": report.get("study_label") or str(key["study_id"]),
        "report_id": str(key["report_id"]),
        "report": report.get("title") or report.get("citation") or str(key["report_id"]),
        "field": ", ".join(
            part
            for part in (spec.get("description") or key["quantity"], endpoint, timepoint, arm)
            if part
        ),
        "quantity": key["quantity"],
        "endpoint": endpoint,
        "timepoint": timepoint,
        "arm": arm,
        "subpopulation": key["subpopulation"],
        "analysis_set": key["analysis_set"],
        "data_cut": key["data_cut"],
        "policy": spec.get("policy", ""),
    }


def _source(quote: Any, boxes: Any) -> tuple[tuple[int, ...], str | None]:
    """An anchor's pages and quote."""
    pages = {b["page"] for b in boxes or [] if isinstance(b, dict) and "page" in b}
    return tuple(sorted(pages)), quote.get("exact") if isinstance(quote, dict) else quote


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
        pages, quote = _source(r["quote"], r["boxes"])
        found.append(
            ExtractedValue(
                **identity(r["key"], r["spec"], r["labels"], r["report"]),
                value=r["value"],
                unit=r["unit"],
                produced_by="manual_edit" if r["edited"] else r["path"],
                proposal_path=r["path"],
                pages=pages,
                quote=quote,
                anchor_id=r["anchor_id"],
                accepted_by=r["reviewer"] or str(r["reviewer_id"]),
                accepted_by_id=r["reviewer_id"],
                accepted_at=r["accepted_at"],
                acceptance_id=r["id"],
                event_seq=r["event_seq"],
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
        writer.writerow(csv_row(v.model_dump(mode="json")))
    return out.getvalue().encode()


def csv_row(value: Mapping[str, Any]) -> list[str]:
    """A value's CSV row from its JSON form, as `as_csv` writes it and `check_bundle` reads it."""
    row = {**value, "pages": " ".join(str(p) for p in value.get("pages") or [])}
    return ["" if row.get(c) is None else str(row[c]) for c in CSV_COLUMNS]


def as_json(values: tuple[ExtractedValue, ...], cutoff_seq: int) -> dict[str, Any]:
    return {
        "cutoff_seq": cutoff_seq,
        "values": [v.model_dump(mode="json") for v in values],
    }


# The offline check of a bundle's copy.


def check_bundle(
    root: Path, events: list[AuditEvent], files: Mapping[str, str], problems: list[str]
) -> None:
    """Compare the bundle's extracted values with the ledger's acceptances (see the module).

    The cutoff is the verified ledger's last event, never what the file says about itself. Each
    value's identity is rebuilt from its acceptance event's semantic key and the bundle's field,
    run, anchor, and acceptance rows, so a row cannot claim another study's or field's value.
    """
    if VALUES_PATH not in files:
        return
    cutoff = max((e.seq for e in events), default=0)
    try:
        data = json.loads((root / VALUES_PATH).read_text())
        values = {v["acceptance_id"]: v for v in data["values"]}
        claimed = int(data["cutoff_seq"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(f"{VALUES_PATH} cannot be read: {e}")
        return
    if claimed != cutoff:
        problems.append(
            f"{VALUES_PATH} says it shows the ledger at event {claimed}, but the bundle's ledger"
            f" ends at event {cutoff}"
        )
    # A review without extraction has empty tables in its bundle, and nothing to compare.
    tables = _tables(root, problems) if values else ({}, {}, {}, {})
    if tables is None:
        return
    fields, runs, anchors, acceptances = tables
    members = {
        str(e.payload.get("actor_id")): e.payload.get("display_name")
        for e in events
        if e.type == MEMBER_ADDED
    }
    latest: dict[str, AuditEvent] = {}
    for e in events:
        if e.type == VALUE_ACCEPTED:
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
        field = fields.get(str(p.get("field_id")))
        run = runs.get(str(p.get("run_id")))
        if field is None or run is None or field["key"] != p.get("key"):
            problems.append(f"{where}: the bundle has no field and run for ledger event {e.seq}")
            continue
        expected = identity(p["key"], field["spec"], field["labels"], run["report"])
        anchor = anchors.get(str(p.get("anchor_id")))
        pages, quote = _source(anchor["quote"], anchor["boxes"]) if anchor else ((), None)
        accepted = acceptances.get(str(v.get("acceptance_id")))
        expected |= {
            "value": p.get("value"),
            "unit": p.get("unit"),
            "produced_by": "manual_edit" if p.get("edited") else p.get("path"),
            "proposal_path": p.get("path"),
            "anchor_id": p.get("anchor_id"),
            "pages": list(pages),
            "quote": quote,
            "accepted_by_id": str(e.actor_id),
            "acceptance_id": accepted["id"] if accepted else None,
        }
        if str(e.actor_id) in members:
            expected["accepted_by"] = members[str(e.actor_id)]
        for name, theirs in expected.items():
            if v.get(name) != theirs:
                problems.append(
                    f"{where} has {name} {v.get(name)!r}, ledger event {e.seq} has {theirs!r}"
                )
        if accepted is None or accepted["event_seq"] != e.seq:
            problems.append(f"{where}: no acceptance row of the bundle belongs to event {e.seq}")
        elif _instant(accepted["accepted_at"]) != _instant(v.get("accepted_at")):
            problems.append(
                f"{where} has accepted_at {v.get('accepted_at')!r}, the bundle has"
                f" {accepted['accepted_at']!r}"
            )
    for seq in sorted(set(by_seq) - seen):
        p = by_seq[seq].payload
        problems.append(
            f"{VALUES_PATH} leaves out the value {p.get('value')!r} accepted at ledger event {seq}"
        )
    if CSV_PATH in files:
        _check_csv(root, values, problems)


def _check_csv(root: Path, values: Mapping[str, Any], problems: list[str]) -> None:
    """Every column of every CSV row must be what the JSON value of the same acceptance gives."""
    try:
        rows = list(csv.reader(io.StringIO((root / CSV_PATH).read_text())))
    except (OSError, ValueError, csv.Error) as e:
        problems.append(f"{CSV_PATH} cannot be read: {e}")
        return
    if not rows or tuple(rows[0]) != CSV_COLUMNS:
        problems.append(f"{CSV_PATH} does not have the columns {', '.join(CSV_COLUMNS)}")
        return
    at = CSV_COLUMNS.index("acceptance_id")
    in_csv = {row[at]: row for row in rows[1:] if len(row) == len(CSV_COLUMNS)}
    if len(in_csv) != len(rows) - 1 or set(in_csv) != set(values):
        problems.append(f"{CSV_PATH} and {VALUES_PATH} list different values")
    for key, row in in_csv.items():
        v = values.get(key)
        if v is None:
            continue
        for column, ours, theirs in zip(CSV_COLUMNS, row, csv_row(v), strict=True):
            if ours != theirs:
                problems.append(
                    f"{CSV_PATH}: {column} of {v.get('field')} is {ours!r}, {VALUES_PATH} has"
                    f" {theirs!r}"
                )


def _instant(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _tables(root: Path, problems: list[str]) -> tuple[dict[str, dict[str, Any]], ...] | None:
    """The bundle's extraction fields, runs, anchors, and acceptances by ID."""
    try:
        db = sqlite3.connect(f"{(root / SQLITE_NAME).resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        problems.append(f"{SQLITE_NAME} cannot be read: {e}")
        return None
    db.row_factory = sqlite3.Row
    try:

        def rows(sql: str, json_columns: tuple[str, ...]) -> dict[str, dict[str, Any]]:
            found = {}
            for r in db.execute(sql):
                row = dict(r)
                for c in json_columns:
                    row[c] = json.loads(row[c]) if isinstance(row[c], str) else row[c]
                found[str(row["id"])] = row
            return found

        return (
            rows("select id, key, spec, labels from extraction_field", ("key", "spec", "labels")),
            rows("select id, report from extraction_run", ("report",)),
            rows("select id, quote, boxes from extraction_anchor", ("quote", "boxes")),
            rows("select id, event_seq, accepted_at from extraction_acceptance", ()),
        )
    except (sqlite3.Error, ValueError) as e:
        problems.append(f"{SQLITE_NAME} has no readable extraction tables: {e}")
        return None
    finally:
        db.close()
